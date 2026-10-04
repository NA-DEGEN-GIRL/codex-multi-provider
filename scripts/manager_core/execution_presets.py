"""Saved, immutable execution choices; credentials stay in registered accounts.

The registry stores account/model identifiers and revision snapshots only. It
does not log in, open desktops, prepare runtimes, or inspect credential files.
Task bindings name an exact revision so editing a preset cannot change a run.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import tomllib
from uuid import uuid4

from .store import atomic_json, identifier, label, now


NATIVE_MODELS = {
    'gpt-6-astra': ('low', 'medium', 'high', 'xhigh', 'max', 'ultra'),
    'gpt-5.6-sol': ('low', 'medium', 'high', 'xhigh', 'max', 'ultra'),
    'gpt-5.6-terra': ('low', 'medium', 'high', 'xhigh', 'max', 'ultra'),
    'gpt-5.6-luna': ('low', 'medium', 'high', 'xhigh', 'max'),
    'gpt-5.5': ('low', 'medium', 'high', 'xhigh'),
}
MAX_BYTES = 8 * 1024 * 1024
# Keep these aligned with native config/execution_presets.rs. History is not
# discarded to fit: a task may hold a durable reference unknown to the manager.
MAX_RUNTIME_BYTES = 1024 * 1024
MAX_RUNTIME_PRESETS = 256
MAX_RUNTIME_ROLES = 256


class PresetError(ValueError):
    """An error safe for the manager UI; never includes credential contents."""


def _revision(value):
    if type(value) is not int or value < 1:
        raise PresetError('실행 프리셋 버전이 올바르지 않습니다.')
    return value


def _task_identity(profile_id, host_id, thread_id):
    for value in (host_id, thread_id):
        if not isinstance(value, str) or not 1 <= len(value) <= 256 or any(ord(c) < 32 for c in value):
            raise PresetError('작업의 호스트와 ID를 확인하세요.')
    value = dict(profile_id=identifier(profile_id), host_id=host_id, thread_id=thread_id)
    key = hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return key, value


class ExecutionPresets:
    def __init__(self, store, providers=None):
        from .providers import ProviderRegistry
        self.store = store
        self.root = store.root
        self.path = store.directory / 'execution-presets.json'
        self.providers = providers or ProviderRegistry(self.root)

    def _read(self):
        if self.path.is_symlink() or not self.path.resolve().is_relative_to(self.root.resolve()):
            raise PresetError('실행 프리셋 저장 경로를 확인하세요.')
        if not self.path.exists():
            return dict(version=1, revision=0, presets={}, defaults={}, bindings={})
        try:
            if self.path.stat().st_size > MAX_BYTES:
                raise ValueError()
            data = json.loads(self.path.read_text(encoding='utf-8-sig'))
            if data.get('version') != 1 or not all(isinstance(data.get(key), dict) for key in ('presets', 'defaults', 'bindings')):
                raise ValueError()
            return data
        except (OSError, ValueError, TypeError, AttributeError):
            raise PresetError('실행 프리셋 저장 정보를 읽을 수 없습니다. 기존 파일은 보존했습니다.') from None

    @staticmethod
    def _validate_runtime_limits(manifest):
        if len(manifest['presets']) > MAX_RUNTIME_PRESETS or len(manifest['roles']) > MAX_RUNTIME_ROLES:
            raise PresetError('저장된 프리셋 이력 또는 역할이 실행기 한도(각 256개)에 도달했습니다. 기존 설정은 보존했습니다.')
        # Both _json and atomic_json publish indented UTF-8 with a final newline.
        encoded = (json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + '\n').encode('utf-8')
        if len(encoded) > MAX_RUNTIME_BYTES:
            raise PresetError('실행 프리셋과 작업 연결 정보가 실행기 저장 한도(1 MiB)에 도달했습니다. 기존 설정은 보존했습니다.')

    def _write(self, data, profile_id=None):
        # Validate the affected owner's full history before committing a save or
        # task binding. Rendering/publication separately checks the final native
        # metadata, whose auth-source paths can be larger than saved ID inputs.
        owners = {identifier(profile_id)} if profile_id is not None else {
            item['profile_id'] for item in data['presets'].values()}
        for owner in owners:
            hosts = {'local'} | {value['host_id'] for value in data['bindings'].values()
                                 if value['profile_id'] == owner}
            for host_id in hosts:
                self._validate_runtime_limits(self._manifest(owner, data, host_id))
        data['revision'] += 1
        if len(json.dumps(data, ensure_ascii=False).encode()) > MAX_BYTES:
            raise PresetError('실행 프리셋 이력이 저장 한도에 도달했습니다.')
        atomic_json(self.path, data)

    def _profile(self, profile_id):
        profile = self.store.profile(identifier(profile_id))
        if profile.get('removed_at') or profile.get('view_only'):
            raise PresetError('실행할 수 있는 등록 계정을 선택하세요.')
        return profile

    @staticmethod
    def _kind(profile):
        mode = profile.get('auth_mode')
        if mode == 'claude_code':
            return 'claude_code'
        if mode == 'external':
            return 'external'
        if mode in (None, 'native', 'source'):
            return 'codex'
        raise PresetError('이 계정의 실행 방식은 프리셋에서 지원하지 않습니다.')

    def _account_provenance(self, profile):
        """Hash trusted account binding metadata, never credential contents."""
        if self._kind(profile) == 'codex':
            home = profile['home'] if profile.get('auth_mode') == 'native' else profile.get('source_home')
            value = dict(home=str(Path(home).resolve()) if home else None,
                         fingerprint=profile.get('account_fingerprint'))
        else:
            from .claude_profiles import settings
            value = settings(profile.get('claude_settings'))
            value.pop('model', None)
            value.pop('reasoning_effort', None)
            value['account_identity'] = profile.get('claude_account_identity')
        return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def _account_choice(self, profile, value):
        from .claude_profiles import MODEL_EFFORTS
        kind = self._kind(profile)
        if kind == 'external':
            raise PresetError('외부 API·로컬 모델은 등록 모델 ID로 선택하세요.')
        choices = MODEL_EFFORTS if kind == 'claude_code' else NATIVE_MODELS
        default = profile.get('claude_settings', {}) if kind == 'claude_code' else {}
        model = value.get('model', default.get('model', 'opus' if kind == 'claude_code' else 'gpt-6-astra'))
        if kind == 'claude_code' and isinstance(model, str):
            model = model.removeprefix('cc-')
        if not isinstance(model, str) or model not in choices:
            raise PresetError('선택한 계정에서 지원하는 모델을 선택하세요.')
        effort = value.get('effort', default.get('reasoning_effort', 'high' if kind == 'claude_code' else 'medium'))
        if effort not in choices[model]:
            raise PresetError('선택한 모델에서 지원하는 추론 강도를 선택하세요.')
        return dict(kind=kind, profile_id=profile['id'], model=model, effort=effort,
                    account_binding=self._account_provenance(profile))

    def _external_choice(self, model_id):
        state = self.providers._read()
        provider, model = self.providers._selected(state, [identifier(model_id)])[0]
        return dict(kind='local' if provider.get('deployment') == 'local' else 'external',
                    model_id=model['id'], model_revision=model['revision'],
                    provider_id=provider['id'], provider_revision=provider['revision'],
                    model=model['wire_model_id'], effort=model['reasoning_effort'])

    def _normalize(self, profile_id, value):
        if not isinstance(value, dict) or set(value) - {'id', 'name', 'main', 'roles', 'expected_revision'}:
            raise PresetError('실행 프리셋 형식을 확인하세요.')
        owner = self._profile(profile_id)
        name = label(value.get('name'))
        main = value.get('main', {})
        if main is None:
            main = {}
        if not isinstance(main, dict) or set(main) - {'model', 'effort'}:
            raise PresetError('주 에이전트 기본값 형식을 확인하세요.')
        if main:
            choice = self._account_choice(owner, main)
            main = {key: choice[key] for key in main}
        roles = value.get('roles', [])
        if not isinstance(roles, list) or len(roles) > 32:
            raise PresetError('하위 에이전트 역할은 32개 이하로 선택하세요.')
        selected, names = [], set()
        for role in roles:
            if not isinstance(role, dict):
                raise PresetError('하위 에이전트 역할 형식을 확인하세요.')
            role_name = label(role.get('name'))
            if role_name in names:
                raise PresetError('역할 이름을 중복 없이 지정하세요.')
            names.add(role_name)
            if 'model_id' in role:
                if set(role) != {'name', 'model_id'}:
                    raise PresetError('등록 모델 역할에는 이름과 모델 ID만 지정하세요.')
                choice = self._external_choice(role['model_id'])
            else:
                if set(role) - {'name', 'profile_id', 'model', 'effort'} or 'profile_id' not in role:
                    raise PresetError('계정 역할에는 명시적인 등록 계정 ID가 필요합니다.')
                choice = self._account_choice(self._profile(role['profile_id']), role)
            selected.append(dict(name=role_name, **choice))
        return dict(profile_id=owner['id'], name=name, main=main, roles=selected)

    @staticmethod
    def _record(data, profile_id, preset_id, revision=None, *, active=False):
        item = data['presets'].get(identifier(preset_id))
        if not item or item.get('profile_id') != identifier(profile_id) or active and item.get('deleted'):
            raise PresetError('이 계정의 저장된 실행 프리셋을 찾을 수 없습니다.')
        number = item['current_revision'] if revision is None else _revision(revision)
        record = item['revisions'].get(str(number))
        if not record:
            raise PresetError('저장된 실행 프리셋 버전을 찾을 수 없습니다.')
        return deepcopy(record)

    def save(self, profile_id, value, expected_revision=None):
        with self.store.locked():
            normalized = self._normalize(profile_id, value)
            data = self._read()
            preset_id = identifier(value['id']) if value.get('id') else str(uuid4())
            previous = data['presets'].get(preset_id)
            expected = expected_revision if expected_revision is not None else value.get('expected_revision')
            if expected is not None and (type(expected) is not int or expected < 0):
                raise PresetError('실행 프리셋의 이전 버전을 확인하세요.')
            if previous:
                self._record(data, profile_id, preset_id, active=True)
                if expected is None or expected != previous['current_revision']:
                    raise PresetError('실행 프리셋이 변경되었습니다. 새 버전을 읽고 다시 저장하세요.')
            elif expected not in (None, 0):
                raise PresetError('실행 프리셋의 이전 버전이 존재하지 않습니다.')
            revision = previous['current_revision'] + 1 if previous else 1
            record = dict(id=preset_id, revision=revision, **normalized, created_at=now())
            item = previous or dict(profile_id=normalized['profile_id'], revisions={})
            item.update(current_revision=revision, deleted=False)
            item['revisions'][str(revision)] = record
            data['presets'][preset_id] = item
            self._write(data, normalized['profile_id'])
            return deepcopy(record)

    def get(self, profile_id, preset_id, revision=None):
        with self.store.locked():
            self._profile(profile_id)
            return self._record(self._read(), profile_id, preset_id, revision)

    def list(self, profile_id):
        from .claude_profiles import MODEL_EFFORTS, MODEL_NAMES
        with self.store.locked():
            owner = self._profile(profile_id)
            data = self._read()
            presets = [deepcopy(item['revisions'][str(item['current_revision'])])
                       for item in data['presets'].values()
                       if item['profile_id'] == owner['id'] and not item.get('deleted')]
            accounts = []
            for profile in self.store.read()['profiles']:
                if profile.get('removed_at') or profile.get('view_only') or profile.get('auth_mode') == 'external':
                    continue
                kind = self._kind(profile)
                choices = MODEL_EFFORTS if kind == 'claude_code' else NATIVE_MODELS
                status = profile.get('claude_status', {}) if kind == 'claude_code' else {}
                accounts.append(dict(id=profile['id'], alias=profile['alias'], kind=kind,
                    auth_state=status.get('state', profile.get('login_state', 'unknown')),
                    logged_in=status.get('logged_in') if kind == 'claude_code' else profile.get('login_state') == 'signed_in',
                    models=[dict(id=model, model=model, name=MODEL_NAMES.get(model, model), efforts=list(efforts))
                            for model, efforts in choices.items()]))
            registry = self.providers.list()
            providers = {provider['id']: provider for provider in registry['providers']}
            models = [dict(id=model['id'], name=model['name'], model=model['wire_model_id'],
                           effort=model['reasoning_effort'], provider_id=model['provider_id'],
                           kind='local' if providers.get(model['provider_id'], {}).get('deployment') == 'local' else 'external',
                           verified=bool(model.get('capabilities', {}).get('verified')),
                           credentials_ready=bool(providers.get(model['provider_id'], {}).get('credentials_ready')))
                      for model in registry['models']]
            return dict(profile_id=owner['id'], presets=presets, owner=next((account for account in accounts if account['id'] == owner['id']), None),
                        default=deepcopy(data['defaults'].get(owner['id'])), accounts=accounts, models=models)

    def delete(self, profile_id, preset_id, expected_revision=None):
        with self.store.locked():
            self._profile(profile_id)
            data = self._read()
            record = self._record(data, profile_id, preset_id, active=True)
            if expected_revision is not None and expected_revision != record['revision']:
                raise PresetError('실행 프리셋이 변경되었습니다. 새 버전을 읽고 다시 삭제하세요.')
            if expected_revision is not None:
                _revision(expected_revision)
            data['presets'][record['id']]['deleted'] = True
            if data['defaults'].get(identifier(profile_id), {}).get('preset_id') == record['id']:
                data['defaults'].pop(identifier(profile_id), None)
            self._write(data, record['profile_id'])
            return dict(deleted=True, id=record['id'], revision=record['revision'])

    def set_default(self, profile_id, preset_id, revision=None):
        with self.store.locked():
            profile_id = self._profile(profile_id)['id']
            data = self._read()
            if preset_id is None:
                selected = None
                data['defaults'].pop(profile_id, None)
            else:
                record = self._record(data, profile_id, preset_id, revision, active=True)
                selected = dict(preset_id=record['id'], revision=record['revision'])
                data['defaults'][profile_id] = selected
            self._write(data, profile_id)
            return dict(profile_id=profile_id, default=deepcopy(selected))

    def _binding(self, data, value):
        if value is None:
            return None
        if value.get('preset_id') is None:
            return dict(value, preset=None, cleared=True, runtime_prepare_required=True)
        return dict(value, preset=self._record(data, value['profile_id'], value['preset_id'], value['revision']),
                    runtime_prepare_required=True)

    def bind(self, profile_id, host_id, thread_id, preset_id, revision=None):
        with self.store.locked():
            self._profile(profile_id)
            key, identity = _task_identity(profile_id, host_id, thread_id)
            data = self._read()
            if preset_id is None:
                value = dict(identity, preset_id=None, revision=None)
                data['bindings'][key] = value
            else:
                record = self._record(data, profile_id, preset_id, revision, active=True)
                value = dict(identity, preset_id=record['id'], revision=record['revision'])
                data['bindings'][key] = value
            self._write(data, profile_id)
            return self._binding(data, value)

    def get_for_task(self, profile_id, host_id, thread_id, bind_default=False):
        with self.store.locked():
            profile_id = self._profile(profile_id)['id']
            key, identity = _task_identity(profile_id, host_id, thread_id)
            data = self._read()
            value = data['bindings'].get(key)
            if value is None and bind_default and (selected := data['defaults'].get(profile_id)):
                value = dict(identity, **selected)
                data['bindings'][key] = value
                self._write(data, profile_id)
            return self._binding(data, value)

    @staticmethod
    def role_id(role):
        payload = json.dumps(role, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        return 'cc_preset_' + hashlib.sha256(payload.encode()).hexdigest()[:24]

    def runtime_path(self, profile_id):
        profile = self._profile(profile_id)
        home = Path(profile['home']).resolve()
        expected = (self.store.directory / 'profiles' / profile['id'] / 'codex').resolve()
        if home != expected or not home.is_relative_to(self.root.resolve()):
            raise PresetError('등록 계정의 실행 프리셋 경로를 확인하세요.')
        return home / 'manager-execution-presets.json'

    def _manifest(self, profile_id, data, host_id='local'):
        presets, roles = [], {}
        selected = data['defaults'].get(profile_id)
        for item in data['presets'].values():
            if item['profile_id'] != profile_id:
                continue
            for record in item['revisions'].values():
                # Native also freezes inherited defaults into task history.
                # Those references need not exist in the manager bindings map;
                # retain every immutable revision, including tombstones, until
                # a future GC can prove no durable task references it.
                ids = []
                for role in record['roles']:
                    role_id = self.role_id(role)
                    roles[role_id] = deepcopy(role)
                    ids.append(role_id)
                presets.append(dict(id=record['id'], revision=record['revision'], profile_id=profile_id,
                                    name=record['name'], main=deepcopy(record['main']), role_ids=ids))
        bindings = {value['thread_id']: (dict(id=value['preset_id'], revision=value['revision'])
                     if value.get('preset_id') is not None else None)
                    for value in data['bindings'].values()
                    if value['profile_id'] == profile_id and value['host_id'] == host_id}
        default = dict(id=selected['preset_id'], revision=selected['revision']) if selected else None
        return dict(schema_version=1, profile_id=profile_id, host_id=host_id, presets=presets,
                    roles=roles, default_preset=default, task_bindings=bindings)

    def _render_role(self, owner_home, role_id, role, *, host=None):
        from .execution_preset_render import render_role
        return render_role(self, owner_home, role_id, role, host=host)

    def render_native_registry(self, profile_id, existing_config, *, host_id='local', _home=None, _host=None):
        """Pure rendering against registered metadata; does not read credentials."""
        from .common import toml_value
        from .providers import _END, _setting, _json

        with self.store.locked():
            profile_id = self._profile(profile_id)['id']
            home = _home or self.runtime_path(profile_id).parent
            if host_id != 'local' and _host is None:
                raise PresetError('계정 프리셋은 해당 PC의 준비된 로컬 런타임에서 사용하세요.')
            data = self._read()
            manifest = self._manifest(profile_id, data, host_id)
            parsed = tomllib.loads(existing_config)
            files, generated_providers, rendered_roles, unavailable_roles = {}, {}, {}, {}
            additions = []
            for role_id, role in manifest['roles'].items():
                try:
                    generated, providers, metadata = self._render_role(home, role_id, role, host=_host)
                except ValueError as error:
                    # A stale saved combination must not stop independent valid
                    # tasks from opening. Its selectors remain unresolved, so
                    # native rejects only a task that explicitly selects it.
                    unavailable_roles[role_id] = str(error)
                    continue
                for name, value in generated.items():
                    if name in files and files[name] != value:
                        raise PresetError('프리셋 역할의 생성 파일이 충돌합니다.')
                    files[name] = value
                for provider_id, definition in providers.items():
                    previous = generated_providers.get(provider_id)
                    if previous is not None and previous != definition:
                        raise PresetError('프리셋 역할의 제공자 설정이 충돌합니다.')
                    generated_providers[provider_id] = definition
                rendered_roles[role_id] = metadata
                definition = dict(description=role['name'], config_file='agents/' + role_id + '.toml')
                old = parsed.get('agents', {}).get(role_id)
                if old is not None and old != definition:
                    raise PresetError('준비된 역할 설정이 변경되었습니다. 프로필을 다시 준비하세요.')
                if old is None:
                    additions.append('[agents.' + role_id + ']\n' + '\n'.join(
                        key + ' = ' + toml_value(value) for key, value in definition.items()))
            for provider_id, definition in generated_providers.items():
                old = parsed.get('model_providers', {}).get(provider_id)
                if old is not None and old != definition:
                    raise PresetError('준비된 제공자 설정이 변경되었습니다. 프로필을 다시 준비하세요.')
                if old is None:
                    additions.append('[model_providers.' + provider_id + ']\n' + '\n'.join(
                        key + ' = ' + toml_value(value) for key, value in definition.items()))
            text = existing_config
            if additions:
                if text.count(_END) != 1:
                    raise PresetError('관리 제공자 설정을 먼저 준비한 뒤 프리셋을 준비하세요.')
                text = text.replace(_END, '\n\n'.join(additions) + '\n' + _END)
            provider_allowlist = set(parsed.get('subagent_model_provider_allowlist', []))
            model_allowlist = set(parsed.get('subagent_model_allowlist', []))
            for role in rendered_roles.values():
                provider_allowlist.add(role['model_provider'])
                model_allowlist.add(role['model_provider'] + '/' + role['model'])
            text = _setting(text, '', 'subagent_model_provider_allowlist', sorted(provider_allowlist))
            text = _setting(text, '', 'subagent_model_allowlist', sorted(model_allowlist))
            # Baseline feature flags are intentionally unchanged. Native preset
            # application activates V2 only for the explicitly selected task.
            tomllib.loads(text)
            files['config.toml'] = text
            unprepared = [dict(id=preset['id'], revision=preset['revision'])
                          for preset in manifest['presets'] if any(role not in rendered_roles for role in preset['role_ids'])]
            manifest['presets'] = [preset for preset in manifest['presets']
                                   if all(role in rendered_roles for role in preset['role_ids'])]
            manifest['roles'] = rendered_roles
            manifest['environment_model_ids'] = sorted({role['model_id'] for role in rendered_roles.values() if 'model_id' in role})
            self._validate_runtime_limits(manifest)
            files['manager-execution-presets.json'] = _json(manifest)
            return dict(files=files, manifest=manifest, path=str(home / 'manager-execution-presets.json'),
                        environment_model_ids=manifest['environment_model_ids'], runtime_prepare_required=bool(unprepared),
                        unprepared_presets=unprepared, unavailable_roles=unavailable_roles)

    def render_for_host(self, profile_id, existing_config, *, host_id, config_home, remote_python, host_identity, remote_cli=None):
        """Render immutable Linux authority separately from mutable selections."""
        if (not isinstance(host_id, str) or not re.fullmatch(r'ssh:[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', host_id)
                or not isinstance(host_identity, str) or not re.fullmatch('[0-9a-f]{64}', host_identity)):
            raise PresetError('SSH 호스트 식별 정보를 확인하세요.')
        for value in (config_home, remote_python):
            if (not isinstance(value, str) or not value.startswith('/') or '\\' in value
                    or any(ord(c) < 32 for c in value) or '..' in PurePosixPath(value).parts
                    or str(PurePosixPath(value)) != value):
                raise PresetError('SSH 실행 경로를 확인하세요.')
        host = dict(host_id=host_id, remote_python=remote_python, host_identity=host_identity,
                    remote_cli=remote_cli, _helper_files={})
        result = self.render_native_registry(profile_id, existing_config, host_id=host_id,
                                             _home=PurePosixPath(config_home), _host=host)
        manifest = result['manifest']
        authority = {key: deepcopy(manifest[key]) for key in
                     ('schema_version', 'profile_id', 'host_id', 'roles', 'environment_model_ids')}
        from .providers import _json
        result['files'].pop('manager-execution-presets.json')
        result['files']['manager-execution-authority.json'] = _json(authority)
        result['authority'] = authority
        result['helper_files'] = host['_helper_files']
        return result

    def manifest_for_prepared_host(self, profile_id, prepared, *, config_home, remote_python, host_identity, remote_cli=None):
        """New selectors may use only byte-equivalent roles in prepared authority."""
        with self.store.locked():
            profile_id = self._profile(profile_id)['id']
            host_id = prepared['host_id']
            data = self._read()
            desired = self._manifest(profile_id, data, host_id)
            host = dict(host_id=host_id, remote_python=remote_python, host_identity=host_identity, remote_cli=remote_cli)
            available = set()
            for role_id, role in desired['roles'].items():
                try:
                    _, _, metadata = self._render_role(PurePosixPath(config_home), role_id, role, host=host)
                except ValueError:
                    continue
                if prepared['roles'].get(role_id) == metadata:
                    available.add(role_id)
            missing = [dict(id=value['id'], revision=value['revision']) for value in desired['presets']
                       if not set(value['role_ids']).issubset(available)]
            desired['presets'] = [value for value in desired['presets'] if set(value['role_ids']).issubset(available)]
            desired['roles'] = deepcopy(prepared['roles'])
            desired['environment_model_ids'] = prepared.get('environment_model_ids', [])
            if 'main_auth' in prepared:
                desired['main_auth'] = deepcopy(prepared['main_auth'])
            self._validate_runtime_limits(desired)
            return dict(manifest=desired, source_revision=data['revision'], runtime_prepare_required=bool(missing),
                        unprepared_presets=missing, environment_model_ids=desired['environment_model_ids'])

    def prepare_runtime(self, profile_id):
        """Publish the launch union after the caller proves the profile inactive.

        Instances owns live-process observation; stored status may be stale and
        is not used as a lifecycle authority. Never call this on a live profile.
        """
        from .common import config_lock
        from .providers import _atomic_bytes

        with self.store.locked():
            path = self.runtime_path(profile_id)
            home = path.parent
            source = (home / 'config.toml').read_text(encoding='utf-8-sig')
            rendered = self.render_native_registry(profile_id, source)
            # Lock order is store/pass before config. Rendering can read account
            # metadata, so it must finish before taking the config lock.
            with config_lock(home):
                if (home / 'config.toml').read_text(encoding='utf-8-sig') != source:
                    raise PresetError('프로필 설정이 변경되었습니다. 다시 준비하세요.')
                for relative, content in rendered['files'].items():
                    target = home / relative
                    if target.is_symlink() or not target.resolve().is_relative_to(home):
                        raise PresetError('프리셋 생성 파일은 등록 프로필 안에 있어야 합니다.')
                    if target.exists() and target.read_text(encoding='utf-8-sig') == content:
                        continue
                    if relative == 'config.toml' and target.read_text(encoding='utf-8-sig') != source:
                        raise PresetError('프로필 설정이 변경되었습니다. 다시 준비하세요.')
                    _atomic_bytes(target, content.encode('utf-8'))
            return {key: value for key, value in rendered.items() if key != 'files'}

    def publish_registry(self, profile_id):
        """Hot-publish references only; never edit a live config or role file."""
        with self.store.locked():
            profile_id = self._profile(profile_id)['id']
            path = self.runtime_path(profile_id)
            if not path.is_file() or path.is_symlink():
                return dict(path=str(path), runtime_prepare_required=True, environment_model_ids=[])
            try:
                prepared = json.loads(path.read_text(encoding='utf-8'))
                if prepared['schema_version'] != 1 or prepared['profile_id'] != profile_id or not isinstance(prepared['roles'], dict):
                    raise ValueError()
            except (OSError, ValueError, TypeError, KeyError):
                raise PresetError('준비된 실행 프리셋 정보를 확인하세요.') from None
            desired = self._manifest(profile_id, self._read())
            available = set()
            for role_id, role in desired['roles'].items():
                try:
                    _, _, metadata = self._render_role(path.parent, role_id, role)
                except ValueError:
                    continue
                if prepared['roles'].get(role_id) == metadata:
                    available.add(role_id)
            eligible = [preset for preset in desired['presets'] if set(preset['role_ids']).issubset(available)]
            allowed = {(preset['id'], preset['revision']) for preset in eligible}
            missing = [dict(id=preset['id'], revision=preset['revision']) for preset in desired['presets']
                       if (preset['id'], preset['revision']) not in allowed]
            desired['presets'] = eligible
            desired['roles'] = prepared['roles']
            desired['environment_model_ids'] = prepared.get('environment_model_ids', [])
            self._validate_runtime_limits(desired)
            atomic_json(path, desired)
            return dict(path=str(path), runtime_prepare_required=bool(missing), unprepared_presets=missing,
                        environment_model_ids=desired['environment_model_ids'])

    def environment_model_ids(self, profile_id):
        path = self.runtime_path(profile_id)
        if not path.is_file():
            return []
        value = json.loads(path.read_text(encoding='utf-8'))
        if value.get('profile_id') != identifier(profile_id) or not isinstance(value.get('environment_model_ids'), list):
            raise PresetError('준비된 프리셋의 모델 연결을 확인하세요.')
        return [identifier(model_id) for model_id in value['environment_model_ids']]
