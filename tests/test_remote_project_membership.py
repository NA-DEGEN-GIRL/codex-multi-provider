import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from uuid import uuid4
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core import remote_project_membership as repair
from manager_core.store import atomic_json


class RemoteMembershipRepairTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / str(uuid4()) / 'codex'
        self.home.mkdir(parents=True)
        self.project, self.thread, self.other = (str(uuid4()) for _ in range(3))
        self.host = 'remote-ssh-discovered:test-host'
        self.state = {'remote-projects': [{'id': self.project, 'hostId': self.host}],
                      repair.ASSIGNMENTS: {self.other: {'projectKind': 'local', 'projectId': str(uuid4())}},
                      repair.PROJECTLESS: [self.thread, self.other],
                      'unrelated': {'draft': 'keep'}}
        atomic_json(self.home / '.codex-global-state.json', self.state)

    def queue(self):
        return repair.queue(self.home, self.project, self.host, [self.thread])

    def test_queue_does_not_edit_running_state_and_apply_only_membership(self):
        original = (self.home / '.codex-global-state.json').read_bytes()
        self.queue()
        self.assertEqual(original, (self.home / '.codex-global-state.json').read_bytes())
        state = copy.deepcopy(self.state)
        result = repair.apply_pending(self.home, state, self.state)
        self.assertEqual(result['assigned'], 1)
        self.assertEqual(state[repair.ASSIGNMENTS][self.thread],
                         dict(projectKind='remote', projectId=self.project, hostId=self.host))
        self.assertEqual(state[repair.PROJECTLESS], [self.other])
        self.assertEqual(state['unrelated'], self.state['unrelated'])
        self.assertEqual(state[repair.ASSIGNMENTS][self.other], self.state[repair.ASSIGNMENTS][self.other])
        self.assertTrue((self.home / repair.PENDING).exists())
        repair.finish(self.home, result)
        self.assertFalse((self.home / repair.PENDING).exists())
        self.assertIsNone(repair.apply_pending(self.home, state, state))

    def test_later_user_move_is_not_overwritten(self):
        self.queue()
        state = copy.deepcopy(self.state)
        state[repair.ASSIGNMENTS][self.thread] = {'projectKind': 'local', 'projectId': str(uuid4())}
        before = copy.deepcopy(state)
        result = repair.apply_pending(self.home, state, before)
        self.assertEqual(result['skipped_changed'], 1)
        self.assertEqual(state, before)

    def test_profile_preparation_persists_before_consuming_request(self):
        from manager_core.app_preferences import prepare
        source = Path(self.tmp.name) / 'source'
        source.mkdir()
        self.queue()
        result = prepare(self.home, source, canonical=True)
        stored = json.loads((self.home / '.codex-global-state.json').read_text())
        self.assertEqual(result['remote_membership']['assigned'], 1)
        self.assertEqual(stored[repair.ASSIGNMENTS][self.thread]['projectId'], self.project)
        self.assertNotIn(self.thread, stored[repair.PROJECTLESS])
        self.assertFalse((self.home / repair.PENDING).exists())

    def test_retry_after_metadata_failure_keeps_assignment_and_clears_projectless(self):
        from manager_core.app_preferences import prepare
        source = Path(self.tmp.name) / 'source'
        source.mkdir()
        atomic_json(source / '.codex-global-state.json', {repair.PROJECTLESS: [self.thread]})
        self.queue()
        def save(path, value):
            if Path(path).name == '.manager-app-preferences.json':
                raise OSError('fixture interrupted metadata write')
            atomic_json(path, value)
        with patch('manager_core.app_preferences.atomic_json', side_effect=save), self.assertRaises(OSError):
            prepare(self.home, source, canonical=True)
        self.assertTrue((self.home / repair.PENDING).exists())
        result = prepare(self.home, source, canonical=True)
        saved = json.loads((self.home / '.codex-global-state.json').read_text())
        self.assertEqual(result['remote_membership']['assigned'], 1)
        self.assertNotIn(self.thread, saved[repair.PROJECTLESS])

    def test_consumed_move_survives_a_second_canonical_preparation(self):
        from manager_core.app_preferences import prepare
        source = Path(self.tmp.name) / 'source'
        source.mkdir()
        donor = {repair.PROJECTLESS: [self.thread, self.other]}
        atomic_json(source / '.codex-global-state.json', donor)
        donor_before = (source / '.codex-global-state.json').read_bytes()
        self.queue()
        prepare(self.home, source, canonical=True)
        self.assertFalse((self.home / repair.PENDING).exists())
        prepare(self.home, source, canonical=True)
        saved = json.loads((self.home / '.codex-global-state.json').read_text())
        self.assertEqual(saved[repair.ASSIGNMENTS][self.thread],
                         dict(projectKind='remote', projectId=self.project, hostId=self.host))
        self.assertNotIn(self.thread, saved[repair.PROJECTLESS])
        self.assertIn(self.other, saved[repair.PROJECTLESS])
        self.assertEqual((source / '.codex-global-state.json').read_bytes(), donor_before)

    def test_remote_projectless_choice_survives_an_absent_local_donor_entry(self):
        from manager_core.app_preferences import prepare
        source = Path(self.tmp.name) / 'source'
        source.mkdir()
        for retained_assignment in (False, True):
            with self.subTest(retained_assignment=retained_assignment):
                state = copy.deepcopy(self.state)
                state[repair.HOSTS] = {self.thread: self.host}
                if retained_assignment:
                    state[repair.ASSIGNMENTS][self.thread] = dict(projectKind='remote', projectId=self.project, hostId=self.host)
                atomic_json(self.home / '.codex-global-state.json', state)
                for _ in range(2):
                    prepare(self.home, source, canonical=True)
                saved = json.loads((self.home / '.codex-global-state.json').read_text())
                self.assertIn(self.thread, saved[repair.PROJECTLESS])
                self.assertEqual(saved[repair.ASSIGNMENTS].get(self.thread), state[repair.ASSIGNMENTS].get(self.thread))
                self.assertEqual(saved[repair.HOSTS][self.thread], self.host)

    def test_remote_ownership_does_not_override_an_explicit_local_assignment(self):
        from manager_core.app_preferences import prepare
        source = Path(self.tmp.name) / 'source'
        source.mkdir()
        donor_assignment = dict(projectKind='local', projectId=str(uuid4()))
        atomic_json(source / '.codex-global-state.json', {repair.ASSIGNMENTS: {self.thread: donor_assignment}})
        for remote in (False, True):
            with self.subTest(remote=remote):
                state = copy.deepcopy(self.state)
                assignment = dict(projectKind='remote', projectId=self.project, hostId=self.host) if remote else dict(projectKind='local', projectId=str(uuid4()))
                state[repair.ASSIGNMENTS][self.thread] = assignment
                state[repair.HOSTS] = {self.thread: self.host}
                state[repair.PROJECTLESS].remove(self.thread)
                atomic_json(self.home / '.codex-global-state.json', state)
                prepare(self.home, source, canonical=True)
                saved = json.loads((self.home / '.codex-global-state.json').read_text())
                self.assertEqual(saved[repair.ASSIGNMENTS][self.thread], assignment if remote else donor_assignment)
                self.assertNotIn(self.thread, saved[repair.PROJECTLESS])

    def test_prepare_preserves_later_explicit_projectless_edit(self):
        from manager_core.app_preferences import prepare
        source = Path(self.tmp.name) / 'source'
        source.mkdir()
        state = copy.deepcopy(self.state)
        state[repair.ASSIGNMENTS][self.thread] = dict(projectKind='remote', projectId=str(uuid4()), hostId=self.host)
        state[repair.PROJECTLESS].remove(self.thread)
        atomic_json(self.home / '.codex-global-state.json', state)
        self.queue()
        state[repair.ASSIGNMENTS].pop(self.thread)
        state[repair.PROJECTLESS].append(self.thread)
        atomic_json(self.home / '.codex-global-state.json', state)
        result = prepare(self.home, source, canonical=True)
        saved = json.loads((self.home / '.codex-global-state.json').read_text())
        self.assertEqual(result['remote_membership']['skipped_changed'], 1)
        self.assertIn(self.thread, saved[repair.PROJECTLESS])
        self.assertNotIn(self.thread, saved[repair.ASSIGNMENTS])

    def test_removed_destination_is_not_recreated(self):
        self.queue()
        state = copy.deepcopy(self.state)
        state['remote-projects'] = []
        before = copy.deepcopy(state)
        result = repair.apply_pending(self.home, state, self.state)
        self.assertTrue(result['skipped_missing_project'])
        self.assertEqual(state, before)

    def test_shared_destination_deletion_wins_over_stale_profile(self):
        from manager_core.app_preferences import prepare
        source = Path(self.tmp.name) / 'source'
        source.mkdir()
        signals = Path(self.tmp.name) / 'signals'
        writer = str(uuid4())
        self.queue()
        atomic_json(signals / 'workspaces' / (writer + '.json'),
                    dict(version=1, projects=[[self.project, 1, writer, None]]))
        result = prepare(self.home, source, canonical=True, signals=signals)
        self.assertTrue(result['remote_membership']['skipped_missing_project'])
        saved = json.loads((self.home / '.codex-global-state.json').read_text())
        self.assertNotIn(self.thread, saved[repair.ASSIGNMENTS])
        self.assertIn(self.thread, saved[repair.PROJECTLESS])

    def test_invalid_and_duplicate_requests_rejected(self):
        with self.assertRaises(ValueError):
            repair.queue(self.home, self.project, 'local', [self.thread])
        with self.assertRaises(ValueError):
            repair.queue(self.home, str(uuid4()), self.host, [self.thread])
        self.queue()
        with self.assertRaises(ValueError):
            self.queue()
        path = self.home / repair.PENDING
        value = json.loads(path.read_text())
        value['profile_id'] = str(uuid4())
        atomic_json(path, value)
        with self.assertRaises(ValueError):
            repair.apply_pending(self.home, self.state, self.state)


if __name__ == '__main__':
    unittest.main()
