"""Host-aware rendering from trusted preset metadata; never reads credentials."""
from __future__ import annotations
import hashlib
from pathlib import Path
import re

def render_role(presets, owner_home, role_id, role, *, host=None):
    from .execution_presets import PresetError
    from .common import toml_value
    from .providers import _GPT_ROLE_INSTRUCTIONS, _INSTRUCTIONS, _json

    files, providers = {}, {}
    metadata = dict(name=role['name'], kind=role['kind'], model=role['model'], effort=role['effort'])
    config = dict(name=role_id, description=role['name'], model=role['model'],
                  model_reasoning_effort=role['effort'], developer_instructions=_INSTRUCTIONS)
    if 'profile_id' in role:
        profile = presets._profile(role['profile_id'])
        if role.get('account_binding') != presets._account_provenance(profile):
            raise PresetError('역할 계정의 로그인 또는 실행 설정이 변경되었습니다. 새 프리셋 버전을 저장하세요.')
    if role['kind'] == 'codex':
        profile = presets._profile(role['profile_id'])
        if presets._kind(profile) != 'codex':
            raise PresetError('저장된 GPT 역할의 계정 종류가 변경되었습니다. 새 프리셋을 저장하세요.')
        auth_home = profile['home'] if profile.get('auth_mode') == 'native' else profile.get('source_home')
        if not auth_home or not Path(auth_home).is_absolute():
            raise PresetError('GPT 역할 계정에 등록된 로그인 경로가 없습니다.')
        metadata.update(model_provider='openai', profile_id=profile['id'])
        if host:
            metadata['auth_source'] = 'manager_proxy'
        else:
            metadata['auth_codex_home'] = str(Path(auth_home).resolve())
        fingerprint = profile.get('account_fingerprint')
        if not isinstance(fingerprint, str) or not re.fullmatch('[0-9a-f]{64}', fingerprint):
            raise PresetError('GPT 역할 계정의 로그인을 확인한 뒤 프리셋을 다시 저장하세요.')
        metadata['expected_account_fingerprint'] = fingerprint
        config.update(model_provider='openai', developer_instructions=_GPT_ROLE_INSTRUCTIONS)
    elif role['kind'] == 'claude_code':
        profile = presets._profile(role['profile_id'])
        if presets._kind(profile) != 'claude_code':
            raise PresetError('저장된 Claude 역할의 계정 종류가 변경되었습니다. 새 프리셋을 저장하세요.')
        if host:
            from .claude_profiles import render_execution_role_for_host
            rendered = render_execution_role_for_host(presets.providers, owner_home, profile,
                role['model'], role['effort'], **{key: value for key, value in host.items() if not key.startswith('_')})
            for name, content in rendered.get('helper_files', {}).items():
                previous = host.setdefault('_helper_files', {}).get(name)
                if previous is not None and previous != content:
                    raise PresetError('SSH 프리셋 도우미 파일이 충돌합니다.')
                host['_helper_files'][name] = content
            metadata.update(rendered.get('auth_metadata', {}))
            metadata['helper_fingerprints'] = {name: hashlib.sha256(content.encode() if isinstance(content, str) else content).hexdigest()
                                               for name, content in rendered.get('helper_files', {}).items()}
        else:
            from .claude_profiles import render_execution_role
            rendered = render_execution_role(presets.providers, owner_home, profile, role['model'], role['effort'])
        providers[rendered['provider_id']] = rendered['provider']
        files.update(rendered['files'])
        metadata.update(model_provider=rendered['provider_id'], model=rendered['model'], profile_id=profile['id'])
        config.update(model_provider=rendered['provider_id'], model=rendered['model'],
                      model_catalog_json=rendered['catalog'], model_context_window=rendered['context_window'])
    else:
        provider, model = presets.providers.execution_preset_external(role)
        if host and provider.get('execution_scope') == 'local':
            raise PresetError('이 PC에서만 실행하는 모델은 SSH 프리셋에서 사용할 수 없습니다.')
        provider_id, definition = presets.providers.execution_preset_http_provider(provider)
        providers[provider_id] = definition
        catalog = 'catalogs/' + role_id + '.json'
        files[catalog] = _json(presets.providers._catalog(model))
        metadata.update(model_provider=provider_id, model_id=role['model_id'])
        config.update(model_provider=provider_id, model_catalog_json=str(owner_home / catalog),
                      model_context_window=model.get('context_window', model.get('capabilities', {}).get('context_window', 32768)))
    # Prepared roles never inherit their parent's provider or its delegation
    # policy. The native preset gate permits only the selected role IDs.
    config['features'] = dict(multi_agent=False, multi_agent_v2=dict(enabled=False))
    files['agents/' + role_id + '.toml'] = '\n'.join(key + ' = ' + toml_value(value) for key, value in config.items()) + '\n'
    metadata['prepared_fingerprint'] = hashlib.sha256(_json(dict(files=files, providers=providers, metadata=metadata)).encode()).hexdigest()
    return files, providers, metadata

