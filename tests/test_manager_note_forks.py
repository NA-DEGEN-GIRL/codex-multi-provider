import copy
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from control_center import ControlCenter
from manager_core import note_forks
from manager_core.remote_catalog import RemoteCatalog, groups
from manager_core.store import Store
from test_manager_remote_catalog import register


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


class SshNoteForkTests(unittest.TestCase):
    parent = '00000000-0000-4000-8000-000000000011'
    child = '00000000-0000-4000-8000-000000000012'
    grandchild = '00000000-0000-4000-8000-000000000013'
    pending = '00000000-0000-4000-8000-000000000014'
    host = 'remote-ssh-discovered:remote-dev'

    def setUp(self):
        note_forks._SEEN.clear()
        note_forks._META.clear()
        note_forks._CATALOGS.clear()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = Store(self.root)
        self.profile = self.store.add_profile('04')
        register(self.store, self.profile)
        self.target = self.root / 'work/control-center/note-forks.json'
        self.rows = [self._row(self.parent, None), self._row(self.child, self.parent),
                     self._row(self.grandchild, self.child), self._row(self.pending)]

    def _row(self, thread_id, *parent):
        row = dict(thread_id=thread_id, source_store_id='manager:' + self.profile['id'], title='SSH task',
                   cwd='/srv/projects/sample-repo', updated_at=1789300000, archived=False)
        if parent:
            row['forked_from_id'] = parent[0]
        return row

    def _catalog(self):
        # The manager's real cache writer, fed by a fixture SSH reader.
        cache = RemoteCatalog(self.root, self.store, object(), reader=lambda _: dict(conversations=copy.deepcopy(self.rows), errors=[]))
        cache._refresh_host(groups(self.store.read())['remote-dev'])
        return cache._path('remote-dev')

    def _key(self, thread_id):
        return 'ssh:remote-dev\0' + thread_id

    def test_catalog_forks_are_published_per_host_alongside_local_forks(self):
        home = self.root / 'codex-home'
        home.mkdir()
        local_parent, local_child = str(uuid4()), str(uuid4())
        with closing(sqlite3.connect(home / 'state_5.sqlite')) as db, db:
            db.execute('CREATE TABLE threads(id TEXT PRIMARY KEY, rollout_path TEXT, source TEXT)')
            # The local thread that shares an SSH fork's id is not a fork.
            for thread_id, parent in ((local_child, local_parent), (self.child, None)):
                rollout = home / (thread_id + '.jsonl')
                rollout.write_text(json.dumps({'type': 'session_meta', 'payload': {
                    'id': thread_id, 'forked_from_id': parent, 'source': 'vscode'}}) + '\n', encoding='utf-8')
                db.execute('INSERT INTO threads VALUES(?,?,?)', (thread_id, str(rollout), 'vscode'))
        state = self.store.read()
        state['sources'].append({'host_id': 'local', 'home': str(home)})
        self._catalog()
        expected = {local_child: local_parent, self._key(self.child): self.parent,
                    self._key(self.grandchild): self.child}
        self.assertEqual(note_forks.refresh(self.root, state), expected)
        self.assertEqual(json.loads(self.target.read_text(encoding='utf-8')), expected)
        # A targeted local read keeps the published SSH forks.
        self.assertEqual(note_forks.refresh(self.root, state, local_child), expected)

    def test_first_note_read_publishes_one_ssh_ancestry_chain(self):
        self._catalog()
        for host in (self.host, 'ssh:remote-dev'):
            with self.subTest(host=host):
                self.target.unlink(missing_ok=True)
                self.assertEqual(note_forks.refresh(self.root, self.store.read(), self.grandchild, host),
                                 {self._key(self.grandchild): self.child, self._key(self.child): self.parent})

    def test_unresolved_or_unknown_ssh_task_waits_without_error_or_file(self):
        self._catalog()
        for thread_id, host in ((self.pending, self.host), (str(uuid4()), self.host),
                                (self.child, 'remote-ssh-discovered:other-host')):
            with self.subTest(thread_id=thread_id, host=host):
                self.assertIsNone(note_forks.refresh(self.root, self.store.read(), thread_id, host))
        self.assertFalse(self.target.exists())
        for host in ('remote-ssh-discovered:', 'cloud:remote-dev'):
            with self.subTest(host=host), self.assertRaises(ValueError):
                note_forks.refresh(self.root, self.store.read(), self.child, host)

    def test_unavailable_catalog_keeps_published_forks_until_host_is_removed(self):
        path = self._catalog()
        published = note_forks.refresh(self.root, self.store.read())
        path.unlink()
        self.assertEqual(note_forks.refresh(self.root, self.store.read()), published)
        # A cache from another source scope is not trusted for new edges.
        self.rows.append(self._row(self.pending, self.grandchild))
        self._catalog()
        register(self.store, self.store.add_profile('05'))
        self.assertEqual(note_forks.refresh(self.root, self.store.read()), published)
        self.store.mutate(lambda data: [p.update(remote_bindings=[]) for p in data['profiles']])
        self.assertEqual(note_forks.refresh(self.root, self.store.read()), {})

    def test_control_center_note_preflight_reads_ssh_fork_metadata(self):
        self._catalog()
        center = ControlCenter(self.root)
        result = center.dispatch('notes.refresh_forks', dict(task=dict(host_id=self.host, thread_id=self.child)))
        self.assertEqual(result, dict(refreshed=True))
        self.assertEqual(json.loads(self.target.read_text(encoding='utf-8')), {self._key(self.child): self.parent})


if __name__ == '__main__':
    unittest.main()
