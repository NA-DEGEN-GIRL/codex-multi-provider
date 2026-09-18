import hashlib
import json
from pathlib import Path
import sys
import sqlite3
import os
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core import membership_proofs
from manager_core.app_preferences import prepare
from manager_core.app_workspace import current_workspace
from manager_core.source_catalog import projection_id


class WorkspaceTests(unittest.TestCase):
    def database(self, home, projects, threads):
        home.mkdir(parents=True, exist_ok=True)
        db = home / 'state_5.sqlite'
        conn = sqlite3.connect(db)
        try:
            conn.executescript('''CREATE TABLE projects(id TEXT PRIMARY KEY,name TEXT,position INTEGER,created_at_ms INTEGER,updated_at_ms INTEGER);
                CREATE TABLE project_roots(project_id TEXT REFERENCES projects(id) ON DELETE CASCADE,position INTEGER,path TEXT);
                CREATE TABLE threads(id TEXT PRIMARY KEY,cwd TEXT,project_id TEXT REFERENCES projects(id) ON DELETE SET NULL,title TEXT);''')
            conn.executemany('INSERT INTO projects VALUES(?,?,0,1,1)', projects)
            conn.executemany('INSERT INTO project_roots VALUES(?,0,?)', [(id,str(home/'workspace')) for id,_ in projects])
            conn.executemany('INSERT INTO threads VALUES(?,?,?,?)', threads)
            conn.commit()
        finally: conn.close()
        return db

    def test_native_membership_move_wins_over_old_legacy_assignment(self):
        with tempfile.TemporaryDirectory() as temp:
            source, target = (Path(temp) / name for name in ('source','target'))
            thread = '11111111-1111-4111-8111-111111111111'
            self.database(source, [('native-old','Old'),('native-new','New')], [(thread,str(source),'native-new','Original task')])
            donor = {'local-projects': {key:dict(id=key,name=key,rootPaths=[str(source)]) for key in ('old','new')},
                'app-server-project-id-by-legacy-project-id-by-host': {'local:'+str(source): {'old':'native-old','new':'native-new'}},
                'app-server-projects-migration-by-host': {'local:'+str(source): dict(projectsMigrated=True,threadAssignmentsMigrated=False)},
                'thread-project-assignments': {thread:dict(projectKind='local',projectId='old')}}
            (source/'.codex-global-state.json').write_text(json.dumps(donor))
            prepare(target,source)
            aliases = json.loads((target/'.manager-project-aliases.json').read_text())
            self.assertEqual(aliases['threadAssignments'][thread], 'new')
            self.assertEqual(aliases['projects'], {'native-old':'old','native-new':'new'})

    def test_native_removal_does_not_resurrect_from_legacy_copy_or_delete_history(self):
        with tempfile.TemporaryDirectory() as temp:
            source, target = (Path(temp) / name for name in ('source','target'))
            thread = '11111111-1111-4111-8111-111111111111'
            source_db = self.database(source, [('native-game','example-audio')], [])
            donor = {'local-projects': {'game':dict(id='game',name='example-audio',rootPaths=[str(source)])},
                'app-server-project-id-by-legacy-project-id-by-host': {'local:'+str(source): {'game':'native-game'}},
                'app-server-projects-migration-by-host': {'local:'+str(source): dict(projectsMigrated=True)}}
            source_state = source/'.codex-global-state.json'
            source_state.write_text(json.dumps(donor)); before = source_state.read_bytes()
            prepare(target,source)
            target_db = self.database(target, [('imported-game','example-audio'),('private','Private')],
                [(thread,str(target),'imported-game','Untouched task')])
            state_path = target/'.codex-global-state.json'
            state = json.loads(state_path.read_text())
            state['local-projects']['game']['updatedAt'] = 999  # Native migration changed the imported copy.
            state['local-projects']['private'] = dict(id='private',name='Private',rootPaths=[])
            state['app-server-project-id-by-legacy-project-id-by-host'] = {'local:'+str(target): {'game':'imported-game'}}
            state_path.write_text(json.dumps(state))
            conn = sqlite3.connect(source_db)
            conn.execute('DELETE FROM projects');conn.commit();conn.close()
            source_before = source_db.read_bytes()
            result = prepare(target,source)
            self.assertEqual(result['workspace']['removed_projects'], 1)
            state = json.loads(state_path.read_text())
            self.assertNotIn('game',state['local-projects']);self.assertNotIn('game',state['project-order'])
            self.assertIn('private',state['local-projects'])
            conn = sqlite3.connect(target_db)
            self.assertEqual(conn.execute('SELECT id FROM projects').fetchall(), [('private',)])
            self.assertEqual(conn.execute('SELECT id,project_id,title FROM threads').fetchall(), [(thread,None,'Untouched task')])
            conn.close()
            self.assertEqual(source_db.read_bytes(), source_before)
            self.assertEqual(source_state.read_bytes(), before)
            self.assertTrue((target/'.manager-removed-projects.json').is_file())
            prepare(target,source)  # Repeated starts must not restore the stale declaration.
            self.assertNotIn('game',json.loads(state_path.read_text())['local-projects'])

    def test_auto_connect_inherits_source_even_before_profile_preparation(self):
        with tempfile.TemporaryDirectory() as temp:
            source, target = (Path(temp) / name for name in ('source', 'target'))
            source.mkdir()
            hosts = ['remote-ssh-discovered:remote-dev', 'remote-ssh-discovered:hp']
            donor = {'codex-managed-remote-connections': [
                {'hostId': host, 'alias': host.split(':')[1]} for host in hosts],
                'remote-connection-auto-connect-by-host-id': dict.fromkeys(hosts, True)}
            (source / '.codex-global-state.json').write_text(json.dumps(donor))
            prepare(target, source, ssh_ready_aliases={'remote-dev'})
            path = target / '.codex-global-state.json'
            state = json.loads(path.read_text())
            self.assertEqual({v['hostId'] for v in state['codex-managed-remote-connections']}, set(hosts))
            self.assertEqual(state['remote-connection-auto-connect-by-host-id'], dict.fromkeys(hosts, True))
            # Migrate the old forced-OFF value; it is not a profile preference.
            state['remote-connection-auto-connect-by-host-id'][hosts[1]] = False
            path.write_text(json.dumps(state))
            owned_path = target / '.manager-app-preferences.json'
            owned = json.loads(owned_path.read_text())
            owned['workspace']['remote-connection-auto-connect-by-host-id'][hosts[1]] = False
            owned_path.write_text(json.dumps(owned))
            prepare(target, source, ssh_ready_aliases={'remote-dev', 'remote-c'})
            self.assertEqual(json.loads(path.read_text())['remote-connection-auto-connect-by-host-id'], dict.fromkeys(hosts, True))

    def test_legacy_folder_grouping_uses_deepest_root_and_respects_projectless(self):
        with tempfile.TemporaryDirectory() as temp:
            source, target = (Path(temp) / name for name in ('source', 'target'))
            source.mkdir()
            nested = source / 'nested'
            threads = ['11111111-1111-4111-8111-111111111111', '22222222-2222-4222-8222-222222222222',
                       '33333333-3333-4333-8333-333333333333']
            donor = {'local-projects': {name: {'id': name, 'name': name, 'rootPaths': [str(root)]}
                                      for name, root in [('root', source), ('nested', nested)]},
                     'projectless-thread-ids': [threads[2]]}
            (source / '.codex-global-state.json').write_text(json.dumps(donor))
            database = source / 'state_5.sqlite'
            with sqlite3.connect(database) as conn:
                conn.execute('CREATE TABLE threads (id TEXT,cwd TEXT)')
                root_path = '\\\\?\\' + str(source) if os.name == 'nt' else str(source)
                conn.executemany('INSERT INTO threads VALUES (?,?)', [(threads[0],root_path), (threads[1],str(nested/'child')), (threads[2],str(nested))])
            conn.close()
            before = database.read_bytes()
            prepare(target, source)
            aliases = json.loads((target / '.manager-project-aliases.json').read_text())
            self.assertEqual(aliases['threadAssignments'], {threads[0]: 'root', threads[1]: 'nested'})
            self.assertEqual(database.read_bytes(), before)

    def test_project_and_ssh_setup_preserves_identity_and_profile_edits(self):
        with tempfile.TemporaryDirectory() as temp:
            source, target = (Path(temp) / name for name in ('source', 'target'))
            source.mkdir()
            thread = '00000000-0000-4000-9000-000000000005'
            host = 'remote-ssh-discovered:remote-dev'
            donor = {'local-projects': {'project': {'id': 'project', 'name': 'Example',
                        'rootPaths': [str(source)], 'createdAt': 1, 'updatedAt': 1}},
                'thread-project-assignments': {thread: {'projectKind': 'local', 'projectId': 'project'}},
                'app-server-project-id-by-legacy-project-id-by-host': {'local:' + str(source): {'project': 'source-server-id'}},
                'app-server-projects-migration-by-host': {'source': {'projectsMigrated': True}},
                'codex-managed-remote-connections': [{'hostId': host, 'displayName': 'remote-dev',
                    'source': 'discovered', 'alias': 'remote-dev', 'hostname': None, 'sshPort': None,
                    'identity': None, 'connectionAnalyticsId': 'source-tracking'}],
                'remote-connection-auto-connect-by-host-id': {host: True},
                'queued-follow-ups': {'secret': 'draft'}, 'selected-remote-host-id': host}
            source_state = source / '.codex-global-state.json'
            source_state.write_text(json.dumps(donor))
            before = source_state.read_bytes()
            result = prepare(target, source)
            self.assertEqual(result['workspace'], {'projects': 1, 'ssh_connections': 1})
            state_path = target / '.codex-global-state.json'
            state = json.loads(state_path.read_text())
            self.assertEqual(state['local-projects'], donor['local-projects'])
            projected = projection_id('legacy:' + hashlib.sha256(str(source).encode()).hexdigest(), thread)
            self.assertEqual(state['thread-project-assignments'], {projected: {'projectKind': 'local', 'projectId': 'project'}})
            self.assertNotIn('app-server-projects-migration-by-host', state)
            self.assertNotIn('queued-follow-ups', state)
            self.assertNotIn('selected-remote-host-id', state)
            self.assertNotIn('connectionAnalyticsId', state['codex-managed-remote-connections'][0])
            aliases = json.loads((target / '.manager-project-aliases.json').read_text())
            self.assertEqual(aliases, {'version': 1, 'sourceHome': str(source),
                'projects': {'source-server-id': 'project'}, 'threadAssignments': {thread: 'project'}})
            state['local-projects']['project']['name'] = 'Profile override'
            state['codex-managed-remote-connections'][0]['connectionAnalyticsId'] = 'target-tracking'
            state['remote-connection-auto-connect-by-host-id'][host] = False
            state_path.write_text(json.dumps(state))
            prepare(target, source)
            updated = json.loads(state_path.read_text())
            self.assertEqual(updated['local-projects']['project']['name'], 'Profile override')
            self.assertFalse(updated['remote-connection-auto-connect-by-host-id'][host])
            self.assertEqual(updated['codex-managed-remote-connections'][0]['connectionAnalyticsId'], 'target-tracking')
            self.assertEqual(source_state.read_bytes(), before)

    def test_next_launch_imports_new_projects_without_resetting_existing_profiles(self):
        with tempfile.TemporaryDirectory() as temp:
            source, target = (Path(temp) / name for name in ('source', 'target'))
            source.mkdir()
            state_path = source / '.codex-global-state.json'
            state_path.write_text('{}')
            prepare(target, source)
            donor = {'local-projects': {'new': {'id': 'new', 'name': 'New', 'rootPaths': [str(source)]}}}
            state_path.write_text(json.dumps(donor))
            prepare(target, source)
            self.assertEqual(json.loads((target / '.codex-global-state.json').read_text())['local-projects'], donor['local-projects'])

    def test_canonical_pending_move_uses_real_thread_id_then_adopts_native_moves(self):
        with tempfile.TemporaryDirectory() as temp:
            source, target = (Path(temp) / name for name in ('source', 'target'))
            source.mkdir()
            thread = '00000000-0000-4000-9000-000000000004'
            host = 'local:' + str(source)
            donor = {'local-projects': {}, 'thread-project-assignments': {
                thread: {'projectKind': 'local', 'projectId': 'audio'}},
                'app-server-project-id-by-legacy-project-id-by-host': {host: {'asset': 'native-asset', 'audio': 'native-audio'}},
                'app-server-projects-migration-by-host': {host: {'projectsMigrated': True,
                    'threadAssignmentsMigrated': False, 'threadAssignmentsReadMigrated': True,
                    'pendingThreadAssignmentIds': [thread]}}}
            path = source / '.codex-global-state.json'
            path.write_text(json.dumps(donor))
            db = source / 'state_5.sqlite'
            with sqlite3.connect(db) as c:
                c.executescript('CREATE TABLE projects(id TEXT,name TEXT,created_at_ms INT,updated_at_ms INT,position INT);'
                    'CREATE TABLE project_roots(project_id TEXT,path TEXT,position INT);'
                    'CREATE TABLE threads(id TEXT,cwd TEXT,project_id TEXT);')
                for i,name in enumerate(('asset','audio')):
                    c.execute('INSERT INTO projects VALUES (?,?,?,?,?)',('native-'+name,name,1,1,i))
                    c.execute('INSERT INTO project_roots VALUES (?,?,?)',('native-'+name,str(source/name),0))
                c.execute('INSERT INTO threads VALUES (?,?,?)',(thread,str(source/'asset'),'native-asset'))
            c.close()
            before = db.read_bytes()
            prepare(target,source,canonical=True)
            state = json.loads((target / path.name).read_text())
            self.assertEqual(state['thread-project-assignments'], donor['thread-project-assignments'])
            self.assertEqual(state['app-server-projects-migration-by-host']['local:'+str(target)],
                             donor['app-server-projects-migration-by-host'][host])
            self.assertEqual(db.read_bytes(),before)
            # After migration, shared native membership wins over retained JSON.
            donor['app-server-projects-migration-by-host'][host].update(
                threadAssignmentsMigrated=True,pendingThreadAssignmentIds=[])
            path.write_text(json.dumps(donor))
            prepare(target,source,canonical=True)
            state = json.loads((target / path.name).read_text())
            self.assertEqual(state['thread-project-assignments'][thread]['projectId'],'asset')
            with sqlite3.connect(db) as c:c.execute('UPDATE threads SET project_id=NULL')
            c.close()
            prepare(target,source,canonical=True)
            state = json.loads((target / path.name).read_text())
            self.assertNotIn(thread,state['thread-project-assignments'])
            self.assertIn(thread,state['projectless-thread-ids'])

    def membership_fixture(self, source, thread, *, native, extra_projects=(), mapping=None, **migration):
        """Source snapshot whose native membership for one task is ambiguous."""
        mapping = {'audio': 'native-audio'} if mapping is None else dict(mapping)
        projects = [('native-audio', 'audio'), *((name, name) for name in extra_projects)]
        self.database(source, projects, [(thread, str(source), native, 'Task')])
        host = 'local:' + str(source)
        return {
            'local-projects': {'audio': dict(id='audio', name='audio', rootPaths=[str(source)])},
            'app-server-project-id-by-legacy-project-id-by-host': {host: mapping},
            'app-server-projects-migration-by-host': {host: dict(projectsMigrated=True, **migration)},
            'thread-project-assignments': {thread: dict(projectKind='local', projectId='audio')},
        }

    def test_pending_assignment_survives_recorded_legacy_migration(self):
        # threadAssignmentsMigrated was recorded true regardless of completion,
        # so it must not let a NULL erase a task the desktop still imports.
        with tempfile.TemporaryDirectory() as temp:
            source, target = (Path(temp) / name for name in ('source', 'target'))
            thread = '00000000-0000-4000-9000-000000000006'
            donor = self.membership_fixture(source, thread, native=None,
                threadAssignmentsMigrated=True, pendingThreadAssignmentIds=[thread])
            (source / '.codex-global-state.json').write_text(json.dumps(donor))
            prepare(target, source)
            state = json.loads((target / '.codex-global-state.json').read_text())
            projected = projection_id('legacy:' + hashlib.sha256(str(source).encode()).hexdigest(), thread)
            self.assertEqual(state['thread-project-assignments'].get(projected),
                             {'projectKind': 'local', 'projectId': 'audio'})
            self.assertNotIn(thread, state.get('projectless-thread-ids', []))
            aliases = json.loads((target / '.manager-project-aliases.json').read_text())
            self.assertEqual(aliases['threadAssignments'], {thread: 'audio'})

    def test_pending_assignment_survives_even_with_confirmed_read_migration(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / 'source'
            thread = '00000000-0000-4000-9000-000000000007'
            original = self.membership_fixture(source, thread, native=None,
                threadAssignmentsMigrated=True, threadAssignmentsReadMigrated=True,
                pendingThreadAssignmentIds=[thread])
            result = current_workspace(source, original)
            self.assertEqual(result['thread-project-assignments'][thread]['projectId'], 'audio')
            self.assertNotIn(thread, result['projectless-thread-ids'])

    def test_unconfirmed_null_keeps_assignment_until_read_migration_or_proof(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / 'source'
            thread = '00000000-0000-4000-9000-000000000008'
            original = self.membership_fixture(source, thread, native=None,
                threadAssignmentsMigrated=True)
            preserved = current_workspace(source, original)
            self.assertEqual(preserved['thread-project-assignments'][thread]['projectId'], 'audio')
            self.assertNotIn(thread, preserved['projectless-thread-ids'])
            # A confirmed read migration turns the same NULL into a real removal.
            migration = original['app-server-projects-migration-by-host']['local:' + str(source)]
            migration['threadAssignmentsReadMigrated'] = True
            cleared = current_workspace(source, original)
            self.assertNotIn(thread, cleared['thread-project-assignments'])
            self.assertIn(thread, cleared['projectless-thread-ids'])
            # A per-task proof is the other accepted confirmation.
            migration.pop('threadAssignmentsReadMigrated')
            signals = Path(temp) / 'record-signals'
            membership_proofs.remember(signals, [thread])
            proven = current_workspace(source, original, signals=signals)
            self.assertNotIn(thread, proven['thread-project-assignments'])
            self.assertIn(thread, proven['projectless-thread-ids'])
            # Without a signals path no proof is read, so an unknown NULL stays safe.
            self.assertIn(thread, current_workspace(source, original)['thread-project-assignments'])

    def test_unknown_native_project_does_not_clear_legacy_assignment(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / 'source'
            thread = '00000000-0000-4000-9000-000000000009'
            # The native row names a project this store no longer describes:
            # not the retained legacy membership's removal evidence.
            original = self.membership_fixture(source, thread, native='native-gone',
                threadAssignmentsMigrated=True, threadAssignmentsReadMigrated=True)
            result = current_workspace(source, original)
            self.assertEqual(result['thread-project-assignments'][thread]['projectId'], 'audio')
            self.assertNotIn(thread, result['projectless-thread-ids'])


if __name__ == '__main__':
    unittest.main()
