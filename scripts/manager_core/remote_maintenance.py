"""Manager orchestration for private SSH lifecycles, separate from catalog reads."""
from .release_code import script_path
from copy import deepcopy
import json
from pathlib import Path
import re
import shlex

from .ssh_shim import ShimError, validate_binding
from .store import atomic_json, identifier
from .updates import UpdateError


# Only these codes may cross the SSH boundary; the helper never returns raw text.
ALLOWED_REMOTE_CODES = ('remote_configuration_changed', 'remote_runtime_exited', 'remote_start_timeout',
                        'remote_idle_binding_missing', 'remote_idle_status_unavailable',
                        'remote_listener_unavailable', 'remote_shutdown_unavailable',
                        'remote_revision_conflict')
# Typed answers that describe a live listener without claiming an idle proof.
IDLE_OBSERVATIONS = ('remote_idle_binding_missing', 'remote_idle_status_unavailable',
                     'remote_listener_unavailable')

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
 if code not in ''' + repr(ALLOWED_REMOTE_CODES) + ''':
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
        path = self.store.directory / 'profiles' / identifier(profile['id']) / 'ssh-bindings.json'
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 256000:
            return True
        manifest = json.loads(path.read_text(encoding='utf-8'))
        if manifest.get('pending_policy_hosts'):
            return True
        requested = {b['alias']: b['revision'] for b in manifest.get('bindings', [])}
        saved = {b['alias']: b['revision'] for b in profile.get('remote_bindings', []) if b.get('prepared') is True}
        if any(alias in saved and requested.get(alias) != saved[alias] for alias in inventory['hosts']):
            return True
        # GUI, clipboard and other manager-only updates must not restart SSH.
        bindings = {b['alias']: b for b in profile.get('remote_bindings', []) if b.get('prepared') is True}
        return any(alias not in bindings or not self.remote.binding_matches_settings(profile, bindings[alias])
                   for alias in inventory['hosts'])

    def verify_settings(self, profile, binding):
        """Backfill old bindings using read-only evidence from immutable config."""
        if self.remote.binding_matches_settings(profile, binding) is True:
            return True
        if (binding.get('prepared') is not True or 'settings_fingerprint' in binding
                or not re.fullmatch(r'[0-9a-f]{64}', binding.get('host_identity') or '')
                or not isinstance(binding.get('runtime_bundle'), str)):
            return False
        files = self.remote.settings_files(profile, binding)
        proof = self.request(binding, 'identity', expected_settings=files,
                             expected_host_identity=binding.get('host_identity'),
                             expected_runtime_bundle=binding.get('runtime_bundle'))
        if proof.get('settings_match') is not True:
            return False
        fingerprint = self.remote._settings_fingerprint(binding, files)
        def save(data):
            current = self.store.profile(profile['id'], data)
            saved = next((b for b in current.get('remote_bindings', []) if b.get('alias') == binding['alias']), None)
            if (current.get('generation') != profile.get('generation') or saved != binding
                    or self.remote.settings_files(current, binding) != files):
                raise UpdateError('remote_generation_changed', '확인 중 SSH 실행 설정이 변경되었습니다.')
            saved['settings_fingerprint'] = fingerprint
        self.store.mutate(save)
        return True

    def reuse_unchanged(self, profile, records):
        """Recover only a wholly unmodified preflight, never a partial restart."""
        if not records or any(r.get('state') != 'unobserved' or r.get('reinspect')
                              or any(key in r for key in ('next_binding', 'exit_proof', 'started', 'process'))
                              for r in records):
            return False
        saved = {b['alias']: b for b in profile.get('remote_bindings', []) if b.get('prepared') is True}
        for record in records:
            binding = record.get('binding')
            if (not binding or binding != record.get('publication_binding')
                    or binding['alias'] not in saved
                    or validate_binding(saved[binding['alias']], profile['id']) != binding):
                return False
            try:
                verified = self.verify_settings(profile, saved[binding['alias']])
            except UpdateError as error:
                # Another descriptor revision is running. That is not an
                # unverified failure: the equivalence path below may still prove
                # the live descriptor carries exactly the desired settings.
                if error.code != 'remote_revision_conflict':
                    raise
                return False
            if not verified:
                return False
        # Matching settings do not certify inactivity. Verify identity only;
        # normal native reconnect/start still rechecks the exact descriptor.
        for record in records:
            try:
                self.request(record['binding'], 'identity')
            except UpdateError as error:
                if error.code != 'remote_revision_conflict':
                    raise
                return False
        path = self.store.directory / 'profiles' / identifier(profile['id']) / 'ssh-bindings.json'
        if path.is_symlink() or path.stat().st_size > 256000:
            raise UpdateError('remote_binding_unknown', 'SSH 연결 설정을 확인해야 합니다.')
        manifest = json.loads(path.read_text(encoding='utf-8'))
        if (manifest.get('profile_id') != profile['id']
                or manifest.get('generation') != profile.get('generation')
                or manifest.get('pending_policy_hosts')):
            raise UpdateError('remote_generation_changed', 'SSH 연결 설정이 확인 중 변경되었습니다.')
        published = [validate_binding(b, profile['id']) for b in manifest.get('bindings', [])]
        if (len({binding['alias'] for binding in published}) != len(published)
                or any(published.count(r['binding']) != 1 for r in records)):
            raise UpdateError('remote_binding_changed', 'SSH 연결 설정이 확인 중 변경되었습니다.')
        return True

    def observe_live(self, binding):
        """Read-only identity of the live revision; no idle proof is required.

        A shared-catalog listener answers this typed observation instead of
        managed idle inventory. Its process is live work and is never stopped
        from this path.
        """
        return self.request(binding, 'inspect', discover_active=True, observe_only=True)

    def reuse_equivalent(self, profile, records):
        """Repoint this generation at live revisions carrying the desired settings.

        An unchanged reconnect must not demand an idle proof, and a live
        descriptor of another revision may still hold exactly the desired
        immutable settings. Only read-only identity evidence is used, and only
        the current generation's binding is repointed at that live revision.
        A genuine settings change returns None so the strict lifecycle path and
        its exit verification stay in charge. Returns the adopted revisions.
        """
        if not records or any(record.get('state') != 'unobserved' or record.get('reinspect')
                              or any(key in record for key in ('next_binding', 'exit_proof', 'started', 'process'))
                              for record in records):
            return None
        saved = {item['alias']: item for item in profile.get('remote_bindings', [])
                 if item.get('prepared') is True}
        # published is the manifest baseline this journal record was built from;
        # saved_baseline is the exact stored descriptor the settings were
        # verified against. Both are revalidated under the state lock.
        adopted, published, saved_baseline, verified, expected = {}, {}, {}, {}, {}
        for record in records:
            alias = record.get('alias')
            prepared = saved.get(alias)
            if (not isinstance(record.get('binding'), dict) or not isinstance(prepared, dict)
                    or record['binding'].get('alias') != alias
                    or not re.fullmatch(r'[0-9a-f]{64}', prepared.get('host_identity') or '')
                    or not isinstance(prepared.get('runtime_bundle'), str)):
                return None
            current = validate_binding(record['binding'], profile['id'])
            trimmed = validate_binding(prepared, profile['id'])
            publication = record.get('publication_binding')
            if (current != {**trimmed, 'revision': current['revision']}
                    or (publication is not None
                        and validate_binding(publication, profile['id']) not in (trimmed, current))):
                return None
            observed = self.observe_live(trimmed)
            live = observed['process']['revision'] if observed['process'] is not None else None
            files = self.remote.settings_files(profile, prepared)
            if live is None or live == trimmed['revision']:
                # Nothing is running, or the live descriptor already is the
                # saved binding: immutable settings evidence alone decides
                # reuse. A publication that still names another revision is
                # repointed at the verified binding instead of being released.
                if not self.verify_settings(profile, prepared):
                    return None
                prepared = self.stored_binding(profile, alias)
                if prepared is None or validate_binding(prepared, profile['id']) != trimmed:
                    return None
                # Keep the full stored descriptor (prepared flag, runtime
                # bundle, host identity, status); only the core comparison
                # below is normalized.
                replacement = prepared
            else:
                # Read-only identity of the live revision with the exact
                # immutable settings expected for this host. A settings change
                # that an existing descriptor cannot prove returns None here.
                proof = self.request({**prepared, 'revision': live}, 'identity', expected_settings=files,
                                     expected_host_identity=prepared.get('host_identity'),
                                     expected_runtime_bundle=prepared.get('runtime_bundle'))
                if proof.get('settings_match') is not True:
                    return None
                replacement = {**prepared, 'revision': live}
            # Compare core descriptors only: the saved metadata (prepared flag,
            # status, fingerprint) must not force a republish of an unchanged
            # publication.
            expected[alias] = current
            if current != validate_binding(replacement, profile['id']):
                adopted[alias] = {**replacement,
                                  'settings_fingerprint': self.remote._settings_fingerprint(replacement, files)}
                published[alias] = current
                saved_baseline[alias] = deepcopy(prepared)
                verified[alias] = files
        # Every cohort member must already be published under its alias. A host
        # this manifest dropped (policy-pending or rewritten) cannot be released
        # as reused: the native SSH cohort would still miss it. Fail closed and
        # let the strict lifecycle path own that host.
        path = self.store.directory / 'profiles' / identifier(profile['id']) / 'ssh-bindings.json'
        try:
            if path.is_symlink() or path.resolve() != path or path.stat().st_size > 256000:
                return None
            manifest = json.loads(path.read_text(encoding='utf-8'))
            listed = [validate_binding(item, profile['id']) for item in manifest.get('bindings', [])]
        except (OSError, ValueError, TypeError, ShimError):
            return None
        if (manifest.get('generation') != profile.get('generation')
                or manifest.get('profile_id') != profile['id']
                or len({item['alias'] for item in listed}) != len(listed)):
            return None
        if manifest.get('pending_policy_hosts'):
            # An alias waitlisted by policy keeps the whole cohort on the
            # strict lifecycle path, exactly like reuse_unchanged requires.
            return None
        for alias, baseline in expected.items():
            if [item for item in listed if item['alias'] == alias] != [baseline]:
                return None
        if adopted:
            self.publish_reused(profile, adopted, published, saved_baseline, verified)
        return {alias: binding['revision'] for alias, binding in adopted.items()}

    def stored_binding(self, profile, alias):
        """Exact stored descriptor for one alias, or None when it is gone."""
        current = self.store.profile(profile['id'])
        for item in current.get('remote_bindings', []):
            if item.get('alias') == alias:
                return deepcopy(item)
        return None

    def publish_reused(self, profile, adopted, published, saved_baseline, verified):
        """Repoint only bindings that still match the verified settings.

        Every claim is revalidated under the state lock before a file is
        written: the generation, the policy revision, the rendered settings
        that were verified, the exact stored descriptor, and the manifest
        baseline this journal record was built from. A concurrent settings save
        or manifest rewrite stops this publication instead of overwriting newer
        state. No lifecycle request is issued.
        """
        path = self.store.directory / 'profiles' / identifier(profile['id']) / 'ssh-bindings.json'
        with self.store.locked():
            current = self.store.profile(profile['id'], self.store.read())
            if current.get('generation') != profile.get('generation'):
                raise UpdateError('remote_generation_changed', 'SSH 연결 설정이 확인 중 변경되었습니다.')
            if current.get('policy', {}).get('desired_revision') != profile.get('policy', {}).get('desired_revision'):
                raise UpdateError('policy_changed', 'SSH 연결 설정을 반영하기 전에 모델 설정이 변경되었습니다.')
            for alias in sorted(adopted):
                if self.remote.settings_files(current, adopted[alias]) != verified[alias]:
                    raise UpdateError('policy_changed', 'SSH 연결 설정을 반영하기 전에 모델 설정이 변경되었습니다.')
            if path.is_symlink() or path.resolve() != path or path.stat().st_size > 256000:
                raise UpdateError('remote_binding_unknown', '실행 중인 SSH 연결 설정을 확인해야 합니다.')
            manifest = json.loads(path.read_text(encoding='utf-8'))
            if manifest.get('generation') != profile['generation'] or manifest.get('profile_id') != profile['id']:
                raise UpdateError('remote_generation_changed', 'SSH 실행 설정이 변경되어 적용을 기다립니다.')
            bindings = [validate_binding(item, profile['id']) for item in manifest.get('bindings', [])]
            if len({item['alias'] for item in bindings}) != len(bindings):
                raise UpdateError('remote_binding_unknown', 'SSH 연결 설정이 중복되었습니다.')
            changed = False
            for alias in sorted(adopted):
                matches = [index for index, item in enumerate(bindings) if item['alias'] == alias]
                replacement = validate_binding(adopted[alias], profile['id'])
                if not matches:
                    # A dropped host keeps the whole cohort on the strict path;
                    # this check must never release a manifest that misses it.
                    raise UpdateError('remote_binding_unknown', '실행 설정에 없는 SSH 서버의 연결 정보를 확인해야 합니다.')
                if len(matches) != 1 or bindings[matches[0]] not in (published[alias], replacement):
                    raise UpdateError('remote_binding_changed', 'SSH 연결 설정이 별도로 변경되었습니다.')
                if bindings[matches[0]] != replacement:
                    bindings[matches[0]] = replacement
                    changed = True
            stored = {}
            for item in current.get('remote_bindings', []):
                alias = item.get('alias')
                if alias in adopted:
                    stored[alias] = deepcopy(item)
            if set(stored) != set(adopted):
                raise UpdateError('remote_binding_unknown', '저장된 SSH 연결 설정을 확인해야 합니다.')
            for alias, item in stored.items():
                if saved_baseline[alias] != item:
                    raise UpdateError('remote_binding_changed', 'SSH 연결 설정이 별도로 변경되었습니다.')

            def save(data):
                latest = self.store.profile(profile['id'], data)
                if latest.get('generation') != profile['generation']:
                    raise UpdateError('remote_generation_changed', 'SSH 연결 설정이 확인 중 변경되었습니다.')
                if latest.get('policy', {}).get('desired_revision') != profile.get('policy', {}).get('desired_revision'):
                    raise UpdateError('policy_changed', 'SSH 연결 설정을 반영하기 전에 모델 설정이 변경되었습니다.')
                merged, replaced = [], set()
                for item in latest.get('remote_bindings', []):
                    alias = item.get('alias')
                    if alias in adopted:
                        if (saved_baseline[alias] != item
                                or self.remote.settings_files(latest, adopted[alias]) != verified[alias]):
                            raise UpdateError('remote_binding_changed', 'SSH 연결 설정이 별도로 변경되었습니다.')
                        merged.append(deepcopy(adopted[alias]))
                        replaced.add(alias)
                    else:
                        merged.append(deepcopy(item))
                if replaced != set(adopted):
                    raise UpdateError('remote_binding_unknown', '저장된 SSH 연결 설정을 확인해야 합니다.')
                latest['remote_bindings'] = merged
            self.store.mutate(save)
            # The state store is the verified claim; the manifest is the
            # publication. One torn write stays detectable and is repaired by
            # the next attempt; it never releases the gate on its own.
            if changed:
                atomic_json(path, {**manifest, 'bindings': bindings})

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
                    'remote_idle_binding_missing': '기존 SSH 작업의 안전한 종료 여부를 확인할 수 없어 적용을 기다립니다.',
                    'remote_idle_status_unavailable': '공유 카탈로그 모드의 SSH 실행은 작업 상태 자동 확인을 제공하지 않습니다. 설정이 같은 연결은 그대로 유지하고, 변경된 설정은 적용을 보류합니다.',
                    'remote_listener_unavailable': 'SSH 실행 프로세스는 남아 있지만 연결 소켓이 닫혔습니다. 종료 결과를 확인해야 합니다.',
                    'remote_shutdown_unavailable': '공유 카탈로그 모드의 SSH 실행은 관리 종료 증명을 지원하지 않아 종료 결과를 확인할 수 없습니다. 실행 중인 원격 작업은 그대로 유지합니다.',
                    'remote_revision_conflict': '다른 SSH 실행 버전이 아직 작업 중입니다. 그 작업이 끝난 뒤 다시 시도해 주세요.',
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
            metadata = {}
            if 'settings_match' in value:
                if operation != 'identity' or 'expected_settings' not in params or type(value['settings_match']) is not bool:
                    raise ValueError()
                metadata['settings_match'] = value['settings_match']
            if 'observation_code' in value:
                if (operation != 'inspect' or params.get('observe_only') is not True
                        or value['idle'] is not False or process is None
                        or value['observation_code'] not in IDLE_OBSERVATIONS):
                    raise ValueError()
                metadata['observation_code'] = value['observation_code']
            if 'runtime_bundle' in value:
                if (process is None or not isinstance(value['runtime_bundle'], str)
                        or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', value['runtime_bundle'])
                        or not re.fullmatch(r'[0-9a-f]{64}', value.get('host_identity', ''))):
                    raise ValueError()
                metadata.update({key: value[key] for key in ('runtime_bundle', 'host_identity')})
            return {'binding': binding, 'process': process, 'idle': value['idle'], 'exited': value['exited'], **metadata,
                    **({'active_binding': actual} if actual != binding else {})}
        except (ValueError, KeyError, TypeError, ShimError):
            stage = {'identity': '기존 실행 확인', 'inspect': '작업 상태 확인',
                     'stop': '종료 확인', 'start': '시작 확인'}.get(operation, '상태 확인')
            raise UpdateError('remote_maintenance_unverified',
                binding['alias'] + ' · ' + stage + ': SSH 실행 상태를 확인하지 못해 설정 적용을 보류했습니다.') from None

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
            # A lost start reply is settled by read-only identity of exactly the
            # requested revision. A shared-catalog listener proves that without
            # idle inventory, and this never claims exit or idle either.
            proof = self.observe_live(entry['next_binding'])
            if proof['process'] is not None and proof.get('active_binding') is None:
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
