from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core import shared_workspaces as workspaces
from manager_core.store import Store, atomic_json
from manager_core.app_workspace import merge_workspace

ID = '11111111-1111-4111-8111-111111111111'
SERVER = '22222222-2222-4222-8222-222222222222'
REMOTE = '33333333-3333-4333-8333-333333333333'
WRITER = '44444444-4444-4444-8444-444444444444'


class SharedWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.original = self.root / 'user/.codex'
        self.original.mkdir(parents=True)
        self.store = Store(self.root)
        self.api = self.store.add_profile('API')
        home = Path(self.api['home']);home.mkdir(parents=True)
        self.project = dict(id=ID, name='API project', rootPaths=['C:/fixture'])
        atomic_json(home / '.codex-global-state.json', {'local-projects': {ID: self.project},
            workspaces.MAP: {'local:' + str(home): {ID: SERVER}}})
        with closing(sqlite3.connect(self.original / 'state_5.sqlite')) as db:
            db.executescript('CREATE TABLE projects(id TEXT,name TEXT,position INTEGER,created_at_ms INTEGER,updated_at_ms INTEGER);'
                            'CREATE TABLE project_roots(project_id TEXT,path TEXT,position INTEGER);'
                            'CREATE TABLE threads(id TEXT,cwd TEXT,project_id TEXT);')
            db.execute('INSERT INTO projects VALUES(?,?,0,1,2)', (SERVER, 'API project'))
            db.execute('INSERT INTO project_roots VALUES(?,?,0)', (SERVER, 'C:/fixture'))
            db.commit()
        self.patch = patch.object(Path, 'home', return_value=self.original.parent)
        self.patch.start();self.addCleanup(self.patch.stop)

    def test_seed_finds_api_created_project_in_common_database_with_its_existing_id(self):
        workspaces.seed_local(self.root)
        rows = workspaces.records(self.store.directory / 'record-signals/local-workspaces', local=True)
        self.assertEqual(rows[ID][2]['serverId'], SERVER)
        self.assertEqual(rows[ID][2]['name'], 'API project')
        self.assertEqual(workspaces.read_state(self.original), {})

    def test_new_profile_has_local_and_ssh_projects_before_desktop_starts(self):
        directory = self.store.directory / 'record-signals/workspaces'
        remote = dict(id=REMOTE, hostId='remote-ssh-discovered:remote-dev', remotePath='/fixture/example-game', label='example-game')
        atomic_json(directory / (WRITER + '.json'), dict(version=1, projects=[[REMOTE, 1, WRITER, remote]]))
        home = Path(self.store.add_profile('06')['home']);home.mkdir(parents=True)
        atomic_json(home / '.codex-global-state.json', {'selected-project': 'private', 'auth': 'private'})
        workspaces.prepare_home(self.root, home)
        data = workspaces.read_state(home)
        self.assertEqual(data['local-projects'][ID]['name'], 'API project')
        self.assertEqual(data[workspaces.MAP]['local:' + str(home)][ID], SERVER)
        self.assertEqual(data['remote-projects'], [remote])
        self.assertEqual(set(data['project-order']), {ID, REMOTE})
        self.assertEqual(data['selected-project'], 'private');self.assertEqual(data['auth'], 'private')

    def test_shared_delete_survives_cold_import_and_delayed_native_project_deletion(self):
        workspaces.seed_local(self.root)
        directory = self.store.directory / 'record-signals/local-workspaces'
        atomic_json(directory / (WRITER + '.json'), dict(version=1, projects=[[ID, 2, WRITER, None]]))
        home = Path(self.api['home'])
        workspaces.prepare_home(self.root, home)
        self.assertEqual(workspaces.read_state(home)['local-projects'], {})
        # Native project/delete supports undo; an old native row isn't a new add.
        workspaces.prepare_home(self.root, home)
        self.assertEqual(workspaces.read_state(home)['local-projects'], {})
        value = {**self.project, 'serverId': SERVER}
        atomic_json(directory / (WRITER + '.json'), dict(version=1, projects=[[ID, 3, WRITER, value]]))
        workspaces.prepare_home(self.root, home)
        self.assertEqual(workspaces.read_state(home)['local-projects'][ID], self.project)

    def test_original_and_new_profile_share_one_legacy_alias_for_same_native_project(self):
        workspaces.seed_local(self.root)
        atomic_json(self.original / '.codex-global-state.json', {'local-projects': {SERVER: {**self.project, 'id': SERVER}},
            workspaces.MAP: {'local:' + str(self.original): {SERVER: SERVER}}})
        workspaces.prepare_home(self.root, self.original)
        self.assertEqual(list(workspaces.read_state(self.original)['local-projects']), [ID])

    def test_preferences_do_not_strip_ssh_order(self):
        current = {'project-order': [REMOTE], 'remote-projects': [{'id': REMOTE}]}
        merge_workspace(current, {}, {}, self.original)
        self.assertEqual(current['project-order'], [REMOTE])

    def test_cold_profile_uses_native_membership_without_changing_selection(self):
        tid = '55555555-5555-4555-8555-555555555555'
        with closing(sqlite3.connect(self.original / 'state_5.sqlite')) as db:
            db.execute('INSERT INTO threads VALUES(?,?,?)', (tid, 'C:/old/project', SERVER));db.commit()
        home = Path(self.api['home'])
        value = workspaces.read_state(home)
        value.update({'projectless-thread-ids': [tid], 'selected-project': 'private'})
        atomic_json(home / '.codex-global-state.json', value)
        workspaces.prepare_home(self.root, home)
        result = workspaces.read_state(home)
        self.assertEqual(result['thread-project-assignments'][tid], dict(projectKind='local', projectId=ID))
        self.assertNotIn(tid, result['projectless-thread-ids'])
        self.assertEqual(result['selected-project'], 'private')
        with closing(sqlite3.connect(self.original / 'state_5.sqlite')) as db:
            db.execute('UPDATE threads SET project_id=NULL WHERE id=?', (tid,));db.commit()
        workspaces.prepare_home(self.root, home)
        self.assertNotIn(tid, workspaces.read_state(home)['thread-project-assignments'])
        self.assertIn(tid, workspaces.read_state(home)['projectless-thread-ids'])


if __name__ == '__main__':
    unittest.main()
