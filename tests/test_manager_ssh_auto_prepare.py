import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock
from uuid import uuid4

from manager_core.ssh_auto_prepare import ensure_binding
from manager_core.ssh_inventory import SshInventory
from manager_core.ssh_shim import ADAPTER_VERSION, ShimError, native_bodies, native_command, route_arguments
from manager_core.store import Store, atomic_json


class AutoPrepareTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root)
        self.profile = self.store.add_profile('04')
        self.pid = self.profile['id']
        self.generation = str(uuid4())
        self.store.mutate(lambda data: self.store.profile(self.pid, data).update(generation=self.generation))
        SshInventory(self.root).prepare(self.pid, self.generation)
        self.path = self.store.directory / 'profiles' / self.pid / 'ssh-bindings.json'
        self.manifest = dict(schema=1, adapter=ADAPTER_VERSION, profile_id=self.pid,
            generation=self.generation, inventory_root=str(self.root), native_compatible=True,
            real_ssh='fixture-ssh', native_cli='codex', bindings=[], auto_prepare_aliases=['dev', 'peer'], selected_model_ids=[])
        atomic_json(self.path, self.manifest)
        self.remote = Mock()
        self.remote.prepare.side_effect = self.prepare

    def prepare(self, alias, pid, home, models, *, reuse_host_runtime=False):
        self.assertTrue(reuse_host_runtime)
        self.assertEqual(pid, self.pid)
        self.assertEqual(models, [])
        self.assertFalse(SshInventory(self.root).coverage(self.store.profile(pid))['complete'])
        base = '/home/fixture/.local/share/codex-control-center/profiles/' + pid
        return dict(alias=alias, profile_id=pid, prepared=True, revision='a'*64,
            remote_python='/usr/bin/python3.12', remote_launcher=base+'/launch.py',
            remote_profile_home=base+'/codex', model_ids=[])

    def args(self, alias='dev', operation='native-probe'):
        return ['-T', alias, native_command(native_bodies()[operation], b'01234567')]

    def ensure(self, args=None):
        return ensure_binding(args or self.args(), self.manifest, self.path, remote=self.remote)

    def test_first_native_connection_prepares_and_routes_once(self):
        result=self.ensure()
        args,event=route_arguments(self.args(),result)
        self.assertEqual(event['alias'],'dev')
        self.assertIn(self.pid,args[-1])
        self.assertEqual(self.store.profile(self.pid)['remote_bindings'][0]['alias'],'dev')
        self.ensure()
        self.assertEqual(self.remote.prepare.call_count,1)
        self.assertTrue(SshInventory(self.root).coverage(self.store.profile(self.pid))['complete'])

    def test_unknown_command_host_and_stop_cannot_provision(self):
        for args in [self.args('unknown'), self.args(operation='native-stop'), ['-T','dev','echo unrelated']]:
            with self.assertRaises(ShimError):self.ensure(args)
        self.remote.prepare.assert_not_called()

    def test_generation_change_during_network_never_publishes_binding(self):
        def changed(*args,**kwargs):
            result=self.prepare(*args,**kwargs)
            self.store.mutate(lambda data:self.store.profile(self.pid,data).update(generation=str(uuid4())))
            return result
        self.remote.prepare.side_effect=changed
        with self.assertRaisesRegex(ShimError,'프로필이 변경'):self.ensure()
        self.assertEqual(json.loads(self.path.read_text())['bindings'],[])
        self.assertFalse(self.store.profile(self.pid).get('remote_bindings'))

    def test_unavailable_host_keeps_preference_and_releases_inventory(self):
        self.remote.prepare.side_effect=lambda *args,**kwargs:dict(prepared=False)
        with self.assertRaisesRegex(ShimError,'자동 준비'):self.ensure()
        self.assertEqual(json.loads(self.path.read_text())['auto_prepare_aliases'],['dev','peer'])
        self.assertTrue(SshInventory(self.root).coverage(self.store.profile(self.pid))['complete'])

    def test_different_hosts_merge_without_losing_binding(self):
        barrier=threading.Barrier(2)
        def concurrent(*args,**kwargs):
            result=self.prepare(*args,**kwargs);barrier.wait(timeout=3);return result
        self.remote.prepare.side_effect=concurrent
        errors=[]
        def call(alias):
            try:self.ensure(self.args(alias))
            except Exception as error:errors.append(error)
        workers=[threading.Thread(target=call,args=(a,)) for a in ['dev','peer']]
        for worker in workers:worker.start()
        for worker in workers:worker.join(timeout=5)
        self.assertFalse(errors,errors)
        self.assertEqual({b['alias'] for b in json.loads(self.path.read_text())['bindings']},{'dev','peer'})


if __name__=='__main__':unittest.main()
