import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from manager_core.app_preferences import prepare
from manager_core.ssh_connection_recovery import AUTO, CONNECTIONS, PENDING, queue

PROFILE = '00000000-0000-4000-9000-000000000007'


class ConnectionRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.source, self.home = root / 'source', root / PROFILE / 'codex'
        self.source.mkdir()
        donor = {CONNECTIONS: [dict(hostId='remote-ssh-discovered:' + alias, alias=alias)
                              for alias in ('dev-host', 'build-host', 'render-host', 'disabled-host')],
                 AUTO: {'remote-ssh-discovered:dev-host': True, 'remote-ssh-discovered:build-host': True,
                        'remote-ssh-discovered:render-host': True, 'remote-ssh-discovered:disabled-host': False}}
        (self.source / '.codex-global-state.json').write_text(json.dumps(donor), encoding='utf-8')
        prepare(self.home, self.source)
        self.path = self.home / '.codex-global-state.json'
        current = self.read()
        current[CONNECTIONS] = [x for x in current[CONNECTIONS] if x['alias'] == 'dev-host']
        current[AUTO] = {'remote-ssh-discovered:dev-host': True}
        self.save(current)
        self.bindings = [dict(alias=alias, profile_id=PROFILE, prepared=True, revision='a' * 64,
            remote_python='/usr/bin/python3', remote_launcher='/home/fixture/.local/share/codex-control-center/profiles/'
            + PROFILE + '/launch.py') for alias in ('build-host', 'render-host')]

    def read(self):
        return json.loads(self.path.read_text(encoding='utf-8'))

    def save(self, state):
        self.path.write_text(json.dumps(state), encoding='utf-8')

    def test_queued_repair_preserves_live_state_then_restores_only_reviewed_hosts_once(self):
        before = self.path.read_bytes()
        queue(self.home, self.source, PROFILE, self.bindings, ['build-host', 'render-host'])
        self.assertEqual(self.path.read_bytes(), before)
        prepare(self.home, self.source)
        state = self.read()
        self.assertEqual({x['alias'] for x in state[CONNECTIONS]}, {'dev-host', 'build-host', 'render-host'})
        self.assertFalse((self.home / PENDING).exists())
        backup = json.loads((self.home / '.manager-ssh-connection-recovery-before.json').read_text(encoding='utf-8'))
        self.assertEqual(backup, json.loads(before))
        state[CONNECTIONS] = [x for x in state[CONNECTIONS] if x['alias'] != 'build-host']
        self.save(state)
        prepare(self.home, self.source)
        self.assertNotIn('build-host', {x['alias'] for x in self.read()[CONNECTIONS]})

    def test_preference_edit_after_queue_cancels_recovery_without_overwriting_it(self):
        queue(self.home, self.source, PROFILE, self.bindings, ['build-host', 'render-host'])
        state = self.read()
        state[AUTO]['remote-ssh-discovered:dev-host'] = False
        self.save(state)
        prepare(self.home, self.source)
        state = self.read()
        self.assertEqual([x['alias'] for x in state[CONNECTIONS]], ['dev-host'])
        self.assertFalse(state[AUTO]['remote-ssh-discovered:dev-host'])
        receipt = json.loads((self.home / '.manager-ssh-connection-recovery-result.json').read_text())
        self.assertEqual(receipt['status'], 'skipped_changed_preferences')

    def test_unprepared_or_missing_donor_alias_never_queues(self):
        with self.assertRaisesRegex(ValueError, 'saved prepared'):
            queue(self.home, self.source, PROFILE, self.bindings, ['unknown'])
        self.assertFalse((self.home / PENDING).exists())


if __name__ == '__main__':
    unittest.main()
