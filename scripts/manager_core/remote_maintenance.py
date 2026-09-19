"""Manager orchestration for private SSH lifecycles, separate from catalog reads."""
from .release_code import script_path
from copy import deepcopy
import json
from pathlib import Path
import shlex

from .ssh_shim import ShimError, validate_binding
from .store import atomic_json, identifier
from .updates import UpdateError


BOOTSTRAP = '''import json,sys,types
try:
 p=json.loads(sys.stdin.buffer.read(262145))
 for name in ('launch','ws_client','native_controller','maintenance'):
  m=types.ModuleType(name);sys.modules[name]=m
  exec(compile(p['modules'][name],'<managed-'+name+'>','exec'),m.__dict__)
 result=sys.modules['maintenance'].dispatch(p['request'])
 print(json.dumps({'ok':True,'result':result}))
except Exception as error:
 code=getattr(error,'code',None)
 if code not in ('remote_configuration_changed','remote_runtime_exited','remote_start_timeout'):
  code='remote_maintenance_unverified'
 print(json.dumps({'ok':False,'code':code}))
 sys.exit(2)
'''


class RemoteMaintenance:
    def __init__(self, root, store, remote):
        self.root, self.store, self.remote = Path(root).resolve(), store, remote

    def pending_on_open(self, profile):
        """Local metadata only: a stopped desktop must reconcile older SSH work."""
        inventory = self.store.read().get('ssh_inventory', {}).get(profile['id'], {})
        if not inventory.get('hosts'):
            return False
        policy = profile['policy']
        if policy.get('launched_revision') != policy.get('desired_revision'):
            return True
        from .startup_updates import selected_manager_proxy
        from .release_code import runtime_revision
        selected = selected_manager_proxy(self.root)
        revision = runtime_revision(selected)
        if revision and profile.get('manager_runtime_revision') != revision:
            return True
        path = self.store.directory / 'profiles' / identifier(profile['id']) / 'ssh-bindings.json'
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 256000:
            return True
        manifest = json.loads(path.read_text(encoding='utf-8'))
        if manifest.get('pending_policy_hosts'):
            return True
        requested = {b['alias']: b['revision'] for b in manifest.get('bindings', [])}
        saved = {b['alias']: b['revision'] for b in profile.get('remote_bindings', []) if b.get('prepared') is True}
        return any(alias in saved and requested.get(alias) != saved[alias] for alias in inventory['hosts'])

    def bindings(self, profile, coverage):
        """Use the running generation's manifest, never newly saved settings."""
        if not isinstance(coverage, dict):
            raise UpdateError('remote_coverage_unknown', '이 프로필의 SSH 실행 범위를 확인해야 합니다.')
        hosts = coverage.get('hosts', [])
        if (coverage.get('maintenance_complete', coverage.get('complete')) is not True
                or coverage.get('generation') != profile.get('generation')
                or not isinstance(hosts, list) or not hosts or hosts[0] != 'local'
                or len(hosts) != len(set(hosts)) or len(hosts) > 33):
            raise UpdateError('remote_coverage_unknown', '이 프로필의 SSH 실행 범위를 확인해야 합니다.')
        if len(hosts) == 1:
            return []
        path = self.store.directory / 'profiles' / identifier(profile['id']) / 'ssh-bindings.json'
        if path.is_symlink() or path.resolve() != path or path.stat().st_size > 256000:
            raise UpdateError('remote_binding_unknown', '실행 중인 SSH 연결 설정을 확인해야 합니다.')
        manifest = json.loads(path.read_text(encoding='utf-8'))
        if manifest.get('generation') != profile['generation'] or manifest.get('profile_id') != profile['id']:
            raise UpdateError('remote_generation_changed', 'SSH 실행 설정이 변경되어 적용을 기다립니다.')
        bindings = [validate_binding(b, profile['id']) for b in manifest.get('bindings', [])]
        if len({b['alias'] for b in bindings}) != len(bindings):
            raise UpdateError('remote_binding_unknown', 'SSH 연결 설정이 중복되었습니다.')
        selected = []
        for alias in hosts[1:]:
            match = next((b for b in bindings if b['alias'] == alias), None)
            if match is None and alias in manifest.get('pending_policy_hosts', []):
                # Policy filtering removed the host from this manifest, but its
                # saved binding still identifies the same private remote profile.
                # Inspection discovers the actual running revision separately.
                saved = [validate_binding(b, profile['id']) for b in profile.get('remote_bindings', [])
                         if b.get('alias') == alias and b.get('prepared') is True]
                if len(saved) == 1:
                    match = saved[0]
            if match is None:
                raise UpdateError('remote_binding_unknown', '이전에 연결한 SSH 서버의 실행 설정이 필요합니다.')
            selected.append(match)
        for operation in coverage.get('operations', []):
            match = next((b for b in selected if b['alias'] == operation.get('alias')), None)
            if (operation.get('operation') != 'native-proxy' or match is None
                    or operation.get('revision') != match['revision']
                    or operation.get('generation') != profile['generation']):
                raise UpdateError('remote_operation_pending', '이 프로필의 SSH 연결 처리가 끝나기를 기다립니다.')
        return sorted(selected, key=lambda b: b['alias'])

    def request(self, binding, operation, **params):
        binding = validate_binding(binding, binding['profile_id'])
        self.remote._alias(binding['alias'])
        modules = {name: script_path(self.root, 'scripts/remote_helpers/' + name + '.py').read_text(encoding='utf-8')
                   for name in ('launch', 'ws_client', 'native_controller', 'maintenance')}
        payload = json.dumps({'modules': modules, 'request': {'binding': binding, 'operation': operation, **params}}).encode()
        if len(payload) > 262144:
            raise UpdateError('remote_request_limit', 'SSH 설정 적용 도우미가 허용 크기를 넘었습니다.')
        result = self.remote._run(binding['alias'], shlex.join([binding['remote_python'], '-c', BOOTSTRAP]),
                                  input=payload, timeout=150 if operation == 'stop' else 60)
        try:
            if len(result.stdout) > 131072:
                raise ValueError()
            response = json.loads(result.stdout)
            if response.get('ok') is False:
                code = response.get('code')
                messages = {
                    'remote_configuration_changed': 'SSH 생성 설정 파일이 변경되어 시작하지 못했습니다. 프로필의 원격 설정 충돌을 확인하세요.',
                    'remote_runtime_exited': 'SSH 런타임이 준비되기 전에 종료되었습니다. 원격 프로필 실행 로그를 확인하세요.',
                    'remote_start_timeout': 'SSH 런타임이 제한 시간 안에 준비되지 않았습니다.',
                }
                if code in messages:
                    raise UpdateError(code, binding['alias'] + ' · ' + operation + ': ' + messages[code])
            value = response['result']
            if (result.returncode or response.get('ok') is not True or not isinstance(value, dict)
                    or type(value.get('idle')) is not bool or type(value.get('exited')) is not bool):
                raise ValueError()
            actual = binding
            if value.get('revision') != binding['revision']:
                if (operation != 'inspect' or params.get('discover_active') is not True
                        or value.get('requested_revision') != binding['revision']):
                    raise ValueError()
                actual = validate_binding({**binding, 'revision': value.get('revision')}, binding['profile_id'])
            process = value.get('process')
            if process is None:
                if value['exited'] is not True or value['idle'] is not True or actual != binding:
                    raise ValueError()
            elif (not isinstance(process, dict) or value['exited'] is not False
                  or type(process.get('pid')) is not int or process['pid'] <= 0
                  or not all(isinstance(process.get(k), str) and process[k] for k in
                             ('process_start', 'boot_id', 'socket'))
                  or process.get('revision') != actual['revision']):
                raise ValueError()
            return {'binding': binding, 'process': process, 'idle': value['idle'], 'exited': value['exited'],
                    **({'active_binding': actual} if actual != binding else {})}
        except (ValueError, KeyError, TypeError, ShimError):
            raise UpdateError('remote_maintenance_unverified', 'SSH 실행 상태 확인이 필요합니다. 설정 적용 결과를 추정하지 않았습니다.') from None

    def snapshot(self, profile, coverage):
        return [self.request(binding, 'inspect', discover_active=True) for binding in self.bindings(profile, coverage)]

    def stop(self, entry):
        return self.request(entry.get('active_binding', entry['binding']), 'stop', expected_process=entry['process'])

    def reconcile(self, entry):
        """Read evidence after a lost result; never replay a lifecycle mutation."""
        if entry.get('state') == 'stop_requested':
            proof = self.request(entry.get('active_binding', entry['binding']), 'inspect')
            if proof['exited'] and proof['idle']:
                entry.update(state='closed', exit_proof=proof)
        elif entry.get('state') == 'start_requested':
            proof = self.request(entry['next_binding'], 'inspect')
            if proof['process'] is not None:
                entry.update(state='started', started=proof)

    def prepare_and_start(self, profile, entries, save_journal, *, lifecycle_guard=None):
        """Prepare the whole cohort first; resume starts only from saved evidence."""
        check_current = lifecycle_guard or (lambda: None)
        check_current()
        if any(entry.get('state') not in ('closed', 'prepared', 'start_requested', 'started') for entry in entries):
            raise UpdateError('remote_exit_unverified', 'SSH 서버 종료 확인 뒤 새 설정을 적용할 수 있습니다.')
        model_ids = profile['policy']['model_ids'] if profile['policy']['enabled'] else []
        revision = profile['policy']['desired_revision']
        from .model_settings import render_options
        options = render_options(profile)
        for entry in entries:
            check_current()
            if entry.get('state') != 'closed':
                if entry.get('target_policy_revision') != revision:
                    raise UpdateError('policy_changed', '이전 SSH 설정 적용 결과를 확인한 뒤 최신 설정을 적용해야 합니다.')
                continue
            alias = entry['binding']['alias']
            binding = self.remote.prepare(alias, profile['id'], profile['home'], model_ids, **options)
            check_current()
            if binding.get('prepared') is not True:
                raise UpdateError('remote_preparation_pending', 'SSH 서버에 새 설정을 준비하지 못했습니다.')
            def save(data):
                current = self.store.profile(profile['id'], data)
                if current.get('generation') != profile.get('generation'):
                    raise UpdateError('remote_generation_changed', 'SSH 준비 중 프로필 실행이 변경되어 이전 결과를 적용하지 않습니다.')
                if render_options(current) != options:
                    raise UpdateError('policy_changed', 'SSH 준비 중 모델 설정이 변경되어 이전 결과를 적용하지 않습니다.')
                if current['policy']['desired_revision'] != profile['policy']['desired_revision']:
                    raise UpdateError('policy_changed', '설정이 다시 변경되었습니다. 최신 설정 적용이 필요합니다.')
                current['remote_bindings'] = [b for b in current.get('remote_bindings', []) if b.get('alias') != alias] + [deepcopy(binding)]
            self.store.mutate(save)
            entry.update(state='prepared', next_binding=validate_binding(binding, profile['id']),
                         target_policy_revision=revision)
            save_journal()
            check_current()

        for entry in entries:
            check_current()
            binding = entry['next_binding']
            if entry['state'] == 'start_requested':
                self.reconcile(entry)
                save_journal()
                check_current()
                if entry['state'] != 'started':
                    raise UpdateError('remote_start_pending', '이전 SSH 시작 요청의 결과 확인이 필요합니다.')
            if entry['state'] == 'started':
                observed = self.request(binding, 'inspect')
                check_current()
                if observed['process'] is None or observed['process'] != entry['started']['process']:
                    raise UpdateError('remote_process_changed', '시작한 SSH 서버의 실행 상태가 변경되었습니다.')
                continue
            # Only a journaled prepared entry can issue a new start. Lost replies
            # stay start_requested and are inspected, never blindly resubmitted.
            entry['state'] = 'start_requested'
            save_journal()
            check_current()
            started = self.request(binding, 'start')
            if started['process'] is None:
                raise UpdateError('remote_start_pending', 'SSH 서버가 실행 중인지 확인해야 합니다.')
            entry.update(state='started', started=started)
            save_journal()
            # A completed request must remain recoverable even if a concurrent
            # edit or new launch invalidated this worker while SSH was pending.
            check_current()

    def retry_pending_starts(self, profile, entries, save_journal):
        """An explicit retry may ensure the SAME immutable revision is running.

        Native start serializes on native-start.lock and returns an existing
        matching daemon. It rejects a different live revision. This is never
        called by background inspection, release reconciliation or task opens.
        """
        for entry in entries:
            if entry.get('state') != 'start_requested':
                continue
            if entry.get('target_policy_revision') != profile['policy']['desired_revision']:
                raise UpdateError('policy_changed', '이전 SSH 시작 결과를 확인한 뒤 최신 설정을 적용해야 합니다.')
            self.reconcile(entry)
            save_journal()
            if entry['state'] == 'started':
                continue
            entry['explicit_start_retries'] = entry.get('explicit_start_retries', 0) + 1
            save_journal()
            started = self.request(entry['next_binding'], 'start')
            if started['process'] is None:
                raise UpdateError('remote_start_pending', 'SSH 서버가 실행 중인지 확인해야 합니다.')
            entry.update(state='started', started=started)
            save_journal()

    def publish_started(self, profile, entries):
        """Publish verified applied bindings, preserving the active generation.

        Called under a maintenance gate, also while reconciling a partial restore.
        Merely saving provider settings or opening a task never enters this path.
        """
        started = [entry for entry in entries if entry.get('state') == 'started']
        if not started:
            return
        path = self.store.directory / 'profiles' / identifier(profile['id']) / 'ssh-bindings.json'
        with self.store.locked():
            current = self.store.profile(profile['id'], self.store.read())
            if current.get('generation') != profile.get('generation'):
                raise UpdateError('remote_generation_changed', 'SSH 설정 반영 중 프로필 실행이 변경되었습니다.')
            if path.is_symlink() or path.resolve() != path or path.stat().st_size > 256000:
                raise UpdateError('remote_binding_unknown', '실행 중인 SSH 연결 설정을 확인해야 합니다.')
            manifest = json.loads(path.read_text(encoding='utf-8'))
            if manifest.get('generation') != profile.get('generation') or manifest.get('profile_id') != profile['id']:
                raise UpdateError('remote_generation_changed', 'SSH 실행 설정이 변경되어 반영을 기다립니다.')
            bindings = [validate_binding(b, profile['id']) for b in manifest.get('bindings', [])]
            if len({b['alias'] for b in bindings}) != len(bindings):
                raise UpdateError('remote_binding_unknown', 'SSH 연결 설정이 중복되었습니다.')
            applied = set()
            for entry in started:
                old = validate_binding(entry['binding'], profile['id'])
                publication = validate_binding(entry.get('publication_binding', entry['binding']), profile['id'])
                new = validate_binding(entry['next_binding'], profile['id'])
                proof = entry.get('started', {})
                if (old['alias'] != new['alias'] or publication['alias'] != old['alias'] or not proof.get('process')
                        or proof['process'].get('revision') != new['revision'] or proof.get('exited') is not False):
                    raise UpdateError('remote_start_unverified', '실제 적용된 SSH 설정을 확인해야 합니다.')
                matches = [i for i, binding in enumerate(bindings) if binding['alias'] == old['alias']]
                if (entry.get('target_policy_revision') != current['policy']['desired_revision']
                        and not (len(matches) == 1 and bindings[matches[0]] == new)):
                    raise UpdateError('policy_changed', 'SSH 연결 설정 반영 전에 모델 정책이 변경되었습니다.')
                if not matches and old['alias'] in manifest.get('pending_policy_hosts', []):
                    bindings.append(new)
                elif len(matches) == 1 and bindings[matches[0]] in (publication, new):
                    bindings[matches[0]] = new
                else:
                    raise UpdateError('remote_binding_changed', 'SSH 연결 설정이 별도로 변경되어 덮어쓰지 않았습니다.')
                applied.add(new['alias'])
            updated = {**manifest, 'bindings': bindings}
            if 'pending_policy_hosts' in updated:
                updated['pending_policy_hosts'] = [alias for alias in updated['pending_policy_hosts'] if alias not in applied]
            if updated != manifest:
                atomic_json(path, updated)
