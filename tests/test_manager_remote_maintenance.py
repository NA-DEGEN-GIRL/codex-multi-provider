"""SSH admission metadata and the runtime's lazy diagnostic registration."""
from contextlib import contextmanager
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4

from manager_core.remote_maintenance import RemoteMaintenance
from manager_core.store import Store, atomic_json
from manager_core.updates import UpdateError

ROOT = Path(__file__).resolve().parents[1]


class RemoteMaintenanceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = Store(self.root)
        self.profile = self.store.add_profile('fixture')
        self.profile['generation'] = str(uuid4())
        self.binding = dict(profile_id=self.profile['id'], alias='remote-dev', revision='a' * 64,
            remote_launcher='/home/test/.local/share/codex-control-center/profiles/' + self.profile['id'] + '/launch.py',
            remote_python='/usr/bin/python3')
        self.path = self.store.directory / 'profiles' / self.profile['id'] / 'ssh-bindings.json'
        atomic_json(self.path, dict(profile_id=self.profile['id'], generation=self.profile['generation'], bindings=[self.binding]))
        self.coverage = dict(complete=False, maintenance_complete=True,
            generation=self.profile['generation'], hosts=['local', 'remote-dev'], operations=[
                dict(operation='native-proxy', alias='remote-dev', revision='a' * 64, generation=self.profile['generation'])])
        self.service = RemoteMaintenance(self.root, self.store, None)

    def test_new_saved_binding_does_not_replace_the_live_generation_binding(self):
        self.profile['remote_bindings'] = [dict(self.binding, revision='b' * 64)]
        self.assertEqual(self.service.bindings(self.profile, self.coverage), [self.binding])
        stale = dict(self.coverage, generation=str(uuid4()))
        with self.assertRaises(UpdateError):
            self.service.bindings(self.profile, stale)

    def test_unknown_proxy_revision_or_another_operation_cannot_claim_coverage(self):
        for changes in ({'revision': 'b' * 64}, {'operation': 'native-start'}, {'generation': str(uuid4())}):
            with self.subTest(changes=changes), self.assertRaises(UpdateError):
                self.service.bindings(self.profile, dict(self.coverage,
                    operations=[dict(self.coverage['operations'][0], **changes)]))

    def test_read_only_reconciliation_never_replays_a_lost_mutation(self):
        self.service.request = MagicMock(return_value={'exited': False, 'idle': True, 'process': {'pid': 12}})
        entry = dict(state='stop_requested', binding=self.binding)
        self.service.reconcile(entry)
        self.assertEqual(entry['state'], 'stop_requested')
        self.service.request.return_value = {'exited': True, 'idle': True, 'process': None}
        self.service.reconcile(entry)
        self.assertEqual(entry['state'], 'closed')
        self.assertEqual([c.args[1] for c in self.service.request.call_args_list], ['inspect', 'inspect'])

    def test_unused_diagnostic_gauges_are_optional_but_current_request_is_required(self):
        native = types.SimpleNamespace(_running=MagicMock(return_value={'pid': 12}),
                                       _instance_lock_released=MagicMock(return_value=True))
        module_spec = importlib.util.spec_from_file_location('maintenance_fixture', ROOT / 'scripts/remote_helpers/maintenance.py')
        module = importlib.util.module_from_spec(module_spec)
        with patch.dict(sys.modules, {'native_controller': native,
                'ws_client': types.SimpleNamespace(WebSocketPipe=MagicMock()),
                'fcntl': types.SimpleNamespace(LOCK_EX=2, LOCK_NB=4, flock=MagicMock())}):
            module_spec.loader.exec_module(module)
        (self.root / 'native-start.lock').write_bytes(b'')
        for values, idle in [({'requests.pending_completion': 1}, True),
                             ({}, False), ({'requests.pending_completion': 2}, False),
                             ({'requests.pending_completion': 1, 'logins.running': 1}, False),
                             ({'requests.pending_completion': 1, 'processes.running': 1}, False)]:
            @contextmanager
            def connection(profile):
                def request(method, params):
                    if method == 'server/diagnostics':
                        return {'process': {'id': 12}, 'gauges': [dict(name='app.managed.' + name, value=value)
                            for name, value in values.items()]}
                    if method == 'thread/loaded/list':
                        return {'data': [], 'nextCursor': None}
                    raise AssertionError(method)
                yield request
            with self.subTest(values=values), patch.object(module, 'connection', connection):
                self.assertEqual(module.inspect(self.root, 'a' * 64)['idle'], idle)


if __name__ == '__main__':
    unittest.main()
