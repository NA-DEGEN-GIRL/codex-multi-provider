"""Same-host SSH handoff, using canonical Linux stores and existing transports."""
from .release_code import script_path
from copy import deepcopy
import json
from pathlib import Path, PurePosixPath
import shlex

from .handoff import HandoffManager, HandoffError, _id
from .runtime_admin import AdminClient
from .ssh_runtime_control import endpoint_id
from .ssh_shim import validate_binding


BOOTSTRAP = '''import json,sys,types
p=json.loads(sys.stdin.buffer.read(262144))
for name in ('authority','managed_sources','handoff_store'):
 m=types.ModuleType(name);sys.modules[name]=m
 exec(compile(p['modules'][name],'<managed-'+name+'>','exec'),m.__dict__)
try:
 result=sys.modules['handoff_store'].Storage().dispatch(p['request'])
 print(json.dumps({'ok':True,'result':result},ensure_ascii=True))
except (OSError,ValueError,RuntimeError,KeyError,TypeError):
 print(json.dumps({'ok':False,'code':'remote_storage_unverified'}))
 sys.exit(2)
'''


class RemoteStorage:
    def __init__(self, root, remote, alias):
        self.root, self.remote, self.alias = Path(root), remote, remote._alias(alias)

    def request(self, binding, operation, **params):
        validate_binding(binding, binding['profile_id'])
        if binding['alias'] != self.alias: raise HandoffError('host_mismatch', '같은 SSH 호스트 안에서만 인계할 수 있습니다.')
        modules = {name: script_path(self.root, path).read_text(encoding='utf-8') for name, path in (
            ('authority', 'scripts/manager_core/authority.py'),
            ('managed_sources', 'scripts/remote_helpers/managed_sources.py'),
            ('handoff_store', 'scripts/remote_helpers/handoff_store.py'))}
        request = dict(operation=operation, profile_id=binding['profile_id'], **params)
        payload = json.dumps(dict(modules=modules, request=request), ensure_ascii=False).encode('utf-8')
        if len(payload) > 262144: raise ValueError('remote storage request too large')
        response = self.remote._run(self.alias, shlex.join([binding['remote_python'], '-c', BOOTSTRAP]),
                                    input=payload, timeout=40)
        if len(response.stdout) > 262144: raise ValueError('remote storage response too large')
        try: result = json.loads(response.stdout)
        except (ValueError, UnicodeError): result = {}
        if response.returncode or result.get('ok') is not True or not isinstance(result.get('result'), dict):
            raise HandoffError('remote_storage_unverified', '원격 원본의 상태를 확인하지 못했습니다. 인계 기록을 보존했습니다.')
        return result['result']


class RemoteHandoffManager(HandoffManager):
    def __init__(self, root, store, remote, alias, *, admin_factory=None, storage=None):
        self.alias = remote._alias(alias)
        self.storage = storage or RemoteStorage(root, remote, self.alias)
        self.remote_identities = {}
        super().__init__(root, store, admin_factory=admin_factory or (
            lambda p: AdminClient(root, endpoint_id(p['id'], self.alias), p['generation'])))
        self.directory = self.directory / ('ssh-' + self.alias)

    def _binding(self, profile):
        matches = [b for b in profile.get('remote_bindings', []) if b.get('alias') == self.alias and b.get('prepared') is True]
        if len(matches) != 1: raise HandoffError('ssh_not_prepared', '이 계정의 SSH 호스트를 먼저 준비해야 합니다.')
        binding = deepcopy(matches[0])
        validate_binding(binding, profile['id'])
        if not binding.get('runtime_bundle') or not binding.get('host_identity'):
            raise HandoffError('ssh_identity_missing', 'SSH 런타임 버전과 호스트 식별자가 필요합니다.')
        return binding

    def _inspect(self, binding, running=False):
        result = self.storage.request(binding, 'inspect', revision=binding['revision'],
            host_identity=binding['host_identity'], runtime_bundle=binding['runtime_bundle'], require_running=running)
        expected = str(PurePosixPath(binding['remote_launcher']).parent / 'codex')
        if result.get('home') != expected or result.get('profile_id') != binding['profile_id']:
            raise HandoffError('ssh_source_identity', 'SSH 원본 저장소의 실제 경로가 바뀌었습니다.')
        return result

    def _home(self, profile):
        binding = self._binding(profile)
        self._inspect(binding)
        # This is an opaque storage handle; never reinterpret Linux paths as
        # Windows paths or copy the canonical files to a local temporary home.
        return binding

    def _reference(self, reference):
        if not isinstance(reference, dict) or reference.get('host_id') != 'ssh:' + self.alias:
            raise HandoffError('host_mismatch', '작업의 SSH 호스트가 일치하지 않습니다.')
        sid = reference.get('source_store_id')
        if not isinstance(sid, str) or not sid.startswith('manager:'):
            raise HandoffError('source_not_writable', '이 호스트의 관리 프로필 원본만 인계할 수 있습니다.')
        profile = self._profile(_id(sid[8:]), require_account=False)
        binding = self._home(profile)
        source = next((s for s in self.store.read()['sources'] if s['id'] == sid and s['host_id'] == reference['host_id']), None)
        if not source or source['home'] != str(PurePosixPath(binding['remote_launcher']).parent / 'codex'):
            raise HandoffError('source_unregistered', '등록된 SSH 원본 저장소가 필요합니다.')
        return dict(thread_id=_id(reference.get('thread_id')), host_id=reference['host_id'], source_store_id=sid), profile, binding

    def _runtime_host(self, reference):
        return 'local'  # Inside the Linux process, its own host is local.

    def _read_authority(self, home, thread_id):
        return self.storage.request(home, 'read', thread_id=thread_id)

    def _admin(self, profile):
        admin, identity = super()._admin(profile)
        binding = self._binding(profile)
        observed = self._inspect(binding, running=True)
        self.remote_identities[id(admin)] = (binding, observed)
        # The common durable journal now records both the Windows transport and
        # the actual Linux daemon identity; they are distinct lifetimes.
        return admin, {**identity, 'remote': deepcopy(observed)}

    def _same_identity(self, admin, expected):
        super()._same_identity(admin, {k: v for k, v in expected.items() if k != 'remote'})
        binding, observed = self.remote_identities[id(admin)]
        if expected.get('remote') != observed or self._inspect(binding, running=True) != observed:
            raise HandoffError('remote_runtime_changed', 'SSH 런타임의 실행 세대가 바뀌었습니다.')

    def _compatible(self, owner, target, source_identity, target_identity):
        a, b = self._binding(owner), self._binding(target)
        return a['runtime_bundle'] == b['runtime_bundle'] and a['host_identity'] == b['host_identity']

    def _manifest(self, profile, required):
        return self.storage.request(self._binding(profile), 'manifest', required=required)['path']

    def _transfer_authority(self, plan, grants, transaction_id):
        # Read the durable exact close response written by the common engine.
        from .handoff import _safe_read
        journal = _safe_read(self.directory / (transaction_id + '.json'), 256 * 1024)
        return self.storage.request(plan['home'], 'transfer', transaction_id=transaction_id,
            expected_grants=grants, target_profile_id=plan['target']['id'], close_proof=journal['close_proof'])['grants']
