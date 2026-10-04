"""Native private credential RPC + SSH auth wrapper + host-native fake Claude.

Runs on Linux against CLAUDE_NATIVE_E2E_RUNTIME. The app-server connection is
stdio and uses the exact ExecutionPresetAuthProxy installed in the SSH pump;
all model traffic goes to the synthetic loopback fixture, never real providers.
"""
import hashlib
import json
import mmap
import os
from pathlib import Path
import sys
import threading
import time
import unittest
from uuid import uuid4

import test_manager_cross_harness_e2e as cross_native
from test_manager_claude_runtime_e2e import Rpc
from test_manager_execution_preset_auth import ParentAuth
from manager_core.claude_profiles import render_for_host
from manager_core.execution_preset_auth import ExecutionPresetAuthProxy
from manager_core.providers import _BEGIN, _END
from manager_core.store import atomic_json
from remote_helpers.install import DISPATCHER


class BrokerRpc(Rpc):
    """The SSH wrapper's message pump over an actual native app-server process."""
    def __init__(self, executable, environment, cwd, broker):
        self.broker = broker
        self.guard = threading.RLock()
        self.stopped = threading.Event()
        self.private_requests = []
        super().__init__(executable, environment, cwd)
        self.poller = threading.Thread(target=self._poll, daemon=True)
        self.poller.start()

    def _deliver(self, outcome):
        for message in outcome.runtime:
            Rpc.send(self, message)
        for message in outcome.frontend:
            self.messages.put(message)

    def send(self, message):
        with self.guard:
            self._deliver(self.broker.process('frontend', message))

    def _read(self):
        try:
            for line in self.process.stdout:
                message = json.loads(line)
                if message.get('method') == 'account/executionPresetAuthTokens/read':
                    self.private_requests.append(message['params'])
                with self.guard:
                    self._deliver(self.broker.process('runtime', message))
        finally:
            self.messages.put(None)

    def _poll(self):
        while not self.stopped.wait(.01):
            with self.guard:
                if self.process.poll() is not None:
                    return
                self._deliver(self.broker.poll())

    def close(self):
        self.stopped.set()
        self.broker.close()
        if hasattr(self, 'poller'):
            self.poller.join(timeout=1)
        super().close()


@unittest.skipUnless(sys.platform.startswith('linux') and os.environ.get('CLAUDE_NATIVE_E2E_RUNTIME'),
                     'Requires Linux and the newly built CLAUDE_NATIVE_E2E_RUNTIME.')
class RemoteCrossHarnessTests(unittest.TestCase):
    environment = cross_native.CrossHarnessTests.environment
    turn = cross_native.CrossHarnessTests.turn
    traces = cross_native.CrossHarnessTests.traces

    @classmethod
    def setUpClass(cls):
        cross_native.CrossHarnessTests.setUpClass()
        with Path(os.environ['CLAUDE_NATIVE_E2E_RUNTIME']).open('rb') as stream, \
             mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as binary:
            if binary.find(b'account/executionPresetAuthTokens/read') < 0:
                raise AssertionError('The configured binary lacks the new SSH account credential RPC.')

    def setUp(self):
        cross_native.CrossHarnessTests.setUp(self)
        self.identity = 'e' * 64
        self.store.mutate(lambda state: self.store.profile(self.claude['id'], state).update(
            claude_account_identity=self.identity))
        self.claude = self.store.profile(self.claude['id'])
        machine = Path('/etc/machine-id').read_text().strip()
        self.host = hashlib.sha256((machine + '\\0' + str(os.getuid()) + '\\0' + str(Path.home())).encode()).hexdigest()
        self.private_reads = []

    def prepare(self, parent_claude, alternate=False):
        self.assertFalse(alternate)
        owner, child = (self.claude, self.gpt) if parent_claude else (self.gpt, self.claude)
        remote_profile = self.base / 'remote' / 'profiles' / owner['id']
        home = remote_profile / 'codex'
        home.mkdir(parents=True)
        preset = self.presets.save(owner['id'], dict(name='SSH fixture', roles=[dict(name='SSH child',
            profile_id=child['id'], model='gpt-6-astra' if parent_claude else 'opus',
            effort='medium' if parent_claude else 'high')]))
        self.presets.set_default(owner['id'], preset['id'], preset['revision'])
        role = self.presets.role_id(preset['roles'][0])
        fake = self.base / 'official-fake-claude'
        source = ('#!' + sys.executable + '\nimport os,sys\n'
                  'if sys.argv[1:]==["--version"]:\n print("2.1.282 (Claude Code)"); raise SystemExit(0)\n'
                  'assert os.environ["CLAUDE_CODE_OAUTH_TOKEN"]=="synthetic-claude-access"\n'
                  'assert "CODEX_MANAGER_CLAUDE_AUTH" not in os.environ\n' + cross_native.FAKE_CLAUDE)
        source = source.replace('TRACE_PATH', repr(str(self.trace))).replace('CHILD_ROLE', repr(role)).replace(
            '__CANCEL_PATH__', repr(str(self.cancel_marker)))
        fake.write_text(source, encoding='utf-8')
        fake.chmod(0o700)
        host = dict(host_id='ssh:fixture', remote_python=sys.executable, host_identity=self.host,
                    remote_cli=dict(path=str(fake), version='2.1.282'))
        base = ('approval_policy="never"\nsandbox_mode="danger-full-access"\ncli_auth_credentials_store="file"\n'
                'chatgpt_base_url=' + json.dumps(self.fixture.base_url) + '\n'
                'openai_base_url=' + json.dumps(self.fixture.base_url) + '\n'
                '[features]\nenable_request_compression=false\nshell_tool=false\napps=false\n'
                '[analytics]\nenabled=false\n[projects.' + json.dumps(str(self.cwd)) + ']\ntrust_level="trusted"\n')
        if parent_claude:
            primary = render_for_host(self.providers, home, owner, base, **host)
            files, helpers, main_auth = primary['files'], primary['helper_files'], primary['main_auth']
        else:
            files = {'config.toml': 'model="gpt-5.5"\nmodel_provider="openai"\nmodel_reasoning_effort="medium"\n'
                      + base + '\n' + _BEGIN + '\n' + _END + '\n'}
            helpers, main_auth = {}, None
            # The parent fixture's own synthetic auth remains independent of all
            # borrowed child accounts. There is no real token in this test.
            (home / 'auth.json').write_bytes(self.auth_bytes)
        generated = self.presets.render_for_host(owner['id'], files['config.toml'],
                                                  config_home=str(home), **host)
        files.update(generated['files'])
        helpers.update(generated['helper_files'])
        authority = generated['authority']
        manifest = generated['manifest']
        if main_auth:
            authority['main_auth'] = main_auth
            manifest['main_auth'] = main_auth
        revision = hashlib.sha256(json.dumps(authority, sort_keys=True).encode()).hexdigest()
        definition = remote_profile / 'definitions' / revision
        helper_root = remote_profile / 'helpers' / revision
        for name, contents in files.items():
            for root in (home, definition):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(contents, encoding='utf-8')
        atomic_json(definition / 'manager-execution-authority.json', authority)
        for name, contents in helpers.items():
            path = helper_root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(contents, encoding='utf-8')
        (remote_profile / 'launch.py').write_text(DISPATCHER, encoding='utf-8')
        atomic_json(remote_profile / 'definitions' / (revision + '.json'), dict(profile_id=owner['id'],
            revision=revision, definition=str(definition), helpers=str(helper_root), host_identity=self.host))
        manifest_path = remote_profile / 'execution-presets' / (revision + '.json')
        atomic_json(manifest_path, manifest)
        binding = dict(alias='fixture', prepared=True, revision=revision, host_identity=self.host)
        self.store.mutate(lambda state: self.store.profile(owner['id'], state).update(
            remote_bindings=[binding], generation=str(uuid4())))
        owner = self.store.profile(owner['id'])
        authority = {**authority, 'revision': revision, 'host_identity': self.host}
        def read_claude(root, profile_id, identity):
            self.private_reads.append((profile_id, identity))
            return dict(accessToken='synthetic-claude-access', expiresAt=int(time.time()) + 3600,
                        accountIdentity=self.identity)
        broker = ExecutionPresetAuthProxy(ParentAuth(), self.store, authority, owner['generation'],
                                          claude_reader=read_claude)
        environment = self.environment(home, owner['id'])
        environment.update(CODEX_MANAGER_EXECUTION_PRESETS=str(manifest_path),
                           CODEX_MANAGER_DEFINITION_REVISION=revision)
        client = BrokerRpc(os.environ['CLAUDE_NATIVE_E2E_RUNTIME'], environment, self.cwd, broker)
        self.addCleanup(client.close)
        client.request('initialize', {'clientInfo': {'name': 'ssh-account-fixture', 'version': '1'},
                                     'capabilities': {'experimentalApi': True}})
        client.send({'method':'initialized', 'params':{}})
        self.rpc, self.connection = client, (environment, self.cwd)
        self.remote_profile = remote_profile
        return client, role, preset

    def _assert_private_credentials(self, parent_claude):
        self.assertTrue(self.private_reads)
        self.assertTrue(all(value == (self.claude['id'], self.identity) for value in self.private_reads))
        requests = self.rpc.private_requests
        self.assertTrue(any(value['kind'] == 'claude' for value in requests))
        self.assertTrue(any(value['roleId'] is None for value in requests) if parent_claude
                        else all(value['roleId'] is not None for value in requests))
        self.assertFalse(list(self.base.rglob('.credentials.json')))
        # Include the account ledger and fake-CLI trace outside the owner profile.
        for path in self.base.rglob('*'):
            if path.is_file() and path.suffix in ('.toml', '.json', '.jsonl'):
                self.assertNotIn(b'synthetic-claude-access', path.read_bytes(), str(path))
        self.assertFalse(any(value.get('method') == 'account/executionPresetAuthTokens/read'
                             for value in self.rpc.pending))

    def test_remote_claude_parent_borrows_main_account_and_controls_gpt_child(self):
        cross_native.CrossHarnessTests.test_claude_parent_controls_native_gpt_child_with_selected_account(self)
        self._assert_private_credentials(True)
        self.assertTrue(any(value['kind'] == 'openai' for value in self.rpc.private_requests))

    def test_remote_gpt_parent_borrows_claude_child_account_and_resumes_session(self):
        cross_native.CrossHarnessTests.test_native_gpt_parent_controls_claude_child_and_preserves_cli_session(self)
        self._assert_private_credentials(False)


if __name__ == '__main__':
    unittest.main()
