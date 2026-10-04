"""Claude agent profile configuration; credentials remain owned by Claude Code."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import sys
import tomllib

EFFORTS = ('low', 'medium', 'high', 'xhigh', 'max', 'ultracode')
# These choices are supported by the installed 2.1.282 CLI. Aliases and account
# access are resolved by Claude; newer IDs need their own compatibility review.
MODEL_NAMES = {'opus': 'Opus', 'sonnet': 'Sonnet', 'fable': 'Fable',
               'claude-opus-5-5': 'Opus 5.5'}
MODEL_EFFORTS = {model: EFFORTS for model in MODEL_NAMES}
MODELS = tuple(MODEL_NAMES)
DEFAULTS = dict(model='opus', reasoning_effort='high', context_window=None,
                auto_compact_percent=None)
HANDOFF_CAP = 240000


def automatic_context_window(model):
    """Budget known native 1M models; unrecognized/older IDs stay conservative.

    This is bridge/catalog metadata, never an override of Claude's model window.
    Claude resolves model aliases, account availability and its tuned compaction.
    """
    model = model.removeprefix('cc-')
    native = ('claude-fable-5', 'claude-fable-5-1', 'claude-sonnet-5',
              'claude-opus-4-7', 'claude-opus-4-8', 'claude-opus-5', 'claude-opus-5-5')
    version = re.sub(r'-\d{8}$', '', model)
    if model in MODELS or version in native:
        return 1000000
    return 200000


def settings(value=None):
    if value is not None and not isinstance(value, dict):
        raise ValueError('Claude 설정 형식을 확인하세요.')
    value = dict(value or {})
    if 'effort' in value and 'reasoning_effort' not in value:
        value['reasoning_effort'] = value.pop('effort')
    unknown = set(value) - set(DEFAULTS)
    if unknown:
        raise ValueError('지원하지 않는 Claude 설정입니다.')
    result = {**DEFAULTS, **value}
    if result['model'] not in MODELS:
        raise ValueError('Claude 모델을 선택하세요.')
    if result['reasoning_effort'] not in MODEL_EFFORTS[result['model']]:
        raise ValueError('Claude 추론 강도를 선택하세요.')
    window, percent = result['context_window'], result['auto_compact_percent']
    if window is not None and (type(window) is not int or not 100000 <= window <= 1000000):
        raise ValueError('Claude 컨텍스트는 100,000~1,000,000 토큰으로 설정하세요.')
    if percent is not None and (type(percent) is not int or not 50 <= percent <= 95):
        raise ValueError('자동 압축 비율은 50~95%로 설정하세요.')
    if percent is not None and (window or automatic_context_window(result['model'])) * percent // 100 < 100000:
        minimum = (10000000 + percent - 1) // percent
        raise ValueError(f'현재 압축 비율에서는 컨텍스트를 {minimum:,} 토큰 이상으로 설정하세요.')
    return result


def render(registry, config_home, profile, existing_config='', *, _runtime=None):
    """Render an agent binding without an Anthropic token or HTTP endpoint."""
    from .providers import _BEGIN, _END, _json, _setting, _strip_provider_block, _toml
    from .store import identifier

    pid = identifier(profile['id'])
    selected = settings(profile.get('settings'))
    context = selected['context_window'] or automatic_context_window(selected['model'])
    percent = selected['auto_compact_percent']
    threshold = context * percent // 100 if percent is not None else None
    # Leave room for CLI tools/system instructions during a cross-provider handoff.
    # A fresh Claude session receives this history once, then only unseen turns.
    # Cap it: the portable summary plus the newest turns carry a long GPT task,
    # and a near-1M first request would spend much of a subscription window.
    handoff_budget = max(10000, min(HANDOFF_CAP, threshold - 32000 if threshold is not None
                                    else context - 64000))
    home = str(config_home)
    # A Windows Python path must never be published to a Linux SSH profile.
    # Remote enrollment needs its own installed CLI and its own native login.
    if _runtime is None and sys.platform == 'win32' and not PureWindowsPath(home).is_absolute():
        raise ValueError('Claude 원격 실행에는 서버의 Claude 설치와 별도 로그인이 필요합니다. Windows 프로필에서는 로컬 작업을 열어 주세요.')
    text = _strip_provider_block(existing_config.replace('\r\n', '\n'))
    tomllib.loads(text)
    model = 'cc-' + selected['model']
    binding = dict(model=model, model_provider='claude_code', provider_name='Claude',
                   **{k: v for k, v in selected.items() if k != 'model'},
                   supported_reasoning_efforts=list(MODEL_EFFORTS[selected['model']]),
                   supported_models={'cc-' + name: list(MODEL_EFFORTS[name]) for name in MODELS},
                   effort_aliases={'minimal': 'low', 'ultra': 'max'}, agent_kind='claude_code')
    key = hashlib.sha256(_json(binding).encode()).hexdigest()[:16]
    catalog_name = f'catalogs/claude-{key}.json'
    # Reuse the catalog shape, then supply Claude's own effort vocabulary and
    # optional threshold instead of the external HTTP provider's fixed limits.
    model_info = {'models': []}
    for name in (selected['model'], *(name for name in MODELS if name != selected['model'])):
        window = selected['context_window'] or automatic_context_window(name)
        info = registry._catalog(dict(wire_model_id='cc-' + name, name='Claude · ' + MODEL_NAMES[name],
            reasoning_effort='high', context_window=window, auto_compact_percent=90,
            capabilities=dict(context_window=window, reasoning_efforts=['high'])))['models'][0]
        info['supported_reasoning_levels'] = [dict(effort=x, description=x) for x in MODEL_EFFORTS[name]]
        info['default_reasoning_level'] = (selected['reasoning_effort']
            if selected['reasoning_effort'] in MODEL_EFFORTS[name] else 'high')
        info['auto_compact_token_limit'] = window * percent // 100 if percent is not None else None
        info['effective_context_window_percent'] = 100
        # The bridge hands PNG/JPEG attachments to Claude as stored files it
        # reads with its Read tool (tasks/claude_code/images.rs).
        info['input_modalities'] = ['text', 'image']
        info['description'] = 'Claude Code 실행. 로그인·도구 실행·압축은 Claude가 처리하며 작업 기록은 공유합니다.'
        info['model_messages']['instructions_template'] = 'Follow the user task and the current project instructions.'
        model_info['models'].append(info)
    # Claude Code owns its agent loop and compaction. Codex must not start a
    # second model loop or advertise unavailable cross-provider subagent roles.
    for section, name, value in [('', 'model', model), ('', 'model_provider', 'claude_code'),
            ('', 'model_reasoning_effort', selected['reasoning_effort']),
            ('', 'model_context_window', context),
            ('', 'model_catalog_json', str((PurePosixPath(config_home) if _runtime else Path(config_home)) / catalog_name)),
            ('', 'subagent_model_provider_allowlist', []), ('', 'subagent_model_allowlist', []),
            ('', 'subagent_model_selection', 'automatic'), ('features', 'multi_agent', False),
            ('features.multi_agent_v2', 'enabled', False),
            ('desktop', 'enabled-reasoning-efforts', list(EFFORTS))]:
        text = _setting(text, section, name, value)
    if threshold is not None:
        text = _setting(text, '', 'model_auto_compact_token_limit', threshold)
    else:
        # Remove the prior manager override when returning to Claude's default.
        head, marker, tail = text.partition('\n[')
        head = re.sub(r'^model_auto_compact_token_limit[ \t]*=.*\n?', '', head, flags=re.MULTILINE)
        text = head + marker + tail
    runner = Path(__file__).resolve().with_name('claude_runner.py')
    args = ['-X', 'utf8', str(runner), 'serve', '--root', str(registry.root), '--profile', pid]
    command = sys.executable
    if _runtime is not None:
        command, args = _runtime['command'], _runtime['args']
    agent = ', '.join([
        'kind = "claude_code"', 'command = ' + _toml(command), 'args = ' + _toml(args),
        'profile_id = ' + _toml(pid), f'handoff_budget_tokens = {handoff_budget}',
        'exclude_dynamic_sections = true'] +
        ([f'context_window = {selected["context_window"]}'] if selected['context_window'] is not None else []) +
        ([f'auto_compact_percent = {percent}'] if percent is not None else []))
    block = '\n'.join([_BEGIN, '[model_providers.claude_code]', 'name = "Claude"',
        'wire_api = "responses"', 'requires_openai_auth = false', 'supports_websockets = false',
        'agent = { ' + agent + ' }', _END, ''])
    config = text.rstrip() + '\n\n' + block
    tomllib.loads(config)
    revision = hashlib.sha256((config + _json(binding)).encode()).hexdigest()[:24]
    return dict(files={'config.toml': config, catalog_name: _json(model_info)}, revision=revision,
                effective_revision=revision, enabled=False, models=[], providers=[], bindings=[], primary=binding)


def render_execution_role(registry, config_home, profile, model, effort):
    """Use a registered Claude account in a prepared role; never clone login."""
    selected = {**settings(profile.get('claude_settings')), 'model': model, 'reasoning_effort': effort}
    rendered = render(registry, config_home, dict(id=profile['id'], settings=selected))
    config = tomllib.loads(rendered['files'].pop('config.toml'))
    provider_id = 'cc_claude_' + profile['id'].replace('-', '')
    provider = config['model_providers']['claude_code']
    provider['name'] = 'Claude · ' + profile.get('alias', 'registered account')
    return dict(provider_id=provider_id, provider=provider, files=rendered['files'],
                model='cc-' + model, effort=effort, catalog=config['model_catalog_json'],
                context_window=config['model_context_window'])


def remote_helper_files():
    """Immutable source-only bundle; no manager state or credentials are copied."""
    package = Path(__file__).resolve().parent
    names = ('claude_runner', 'claude_auth', 'claude_protocol', 'claude_profiles',
             'claude_permission_mcp', 'claude_delegation', 'claude_delegation_mcp', 'store',
             'claude_skills', 'personal_skills', 'common')
    files = {'manager_core/__init__.py': ''}
    files.update({'manager_core/' + name + '.py': (package / (name + '.py')).read_text(encoding='utf-8')
                  for name in names})
    files['claude_remote.py'] = (package.parent / 'remote_helpers/claude_remote.py').read_text(encoding='utf-8')
    return files


def render_for_host(registry, config_home, profile, existing_config='', *, host_id,
                    remote_python, host_identity, remote_cli=None):
    """Render a host-native official CLI binding with access-only broker auth."""
    from .store import identifier
    home = PurePosixPath(config_home)
    owner = identifier(home.parent.name)
    target = identifier(profile['id'])
    identity = profile.get('claude_account_identity')
    if (profile.get('auth_mode') != 'claude_code' or not isinstance(identity, str)
            or not re.fullmatch(r'[0-9a-f]{64}', identity)):
        raise ValueError('원격 Claude 실행 전에 선택한 계정의 로컬 로그인을 확인해 주세요.')
    if (not isinstance(host_id, str) or not host_id.startswith('ssh:')
            or not isinstance(host_identity, str) or not re.fullmatch(r'[0-9a-f]{64}', host_identity)):
        raise ValueError('Claude SSH 호스트 식별 정보를 확인해 주세요.')
    remote_cli = remote_cli or {}
    cli_path, version = remote_cli.get('path'), remote_cli.get('version')
    match = re.fullmatch(r'(\d+)\.(\d+)\.(\d+)', version or '')
    for path in (str(home), remote_python, cli_path):
        if (not isinstance(path, str) or not PurePosixPath(path).is_absolute()
                or '\\' in path or ':' in path or '..' in PurePosixPath(path).parts
                or any(ord(char) < 32 for char in path)):
            raise ValueError('SSH 서버의 Python 및 Claude 실행 파일 경로를 확인해 주세요.')
    if not match or tuple(map(int, match.groups())) < (2, 1, 282):
        raise ValueError('SSH 서버에 Claude Code 2.1.282 이상을 준비해 주세요.')
    selected = settings(profile.get('claude_settings', profile.get('settings')))
    relative = 'claude-bindings/' + target + '.json'
    binding = dict(schema_version=1, owner_profile_id=owner, target_profile_id=target,
                   host_id=host_id, host_identity=host_identity, expected_account_identity=identity,
                   cli_path=cli_path, cli_version=version, settings=selected)
    runtime = dict(command=remote_python, args=['-X', 'utf8', str(home.parent / 'launch.py'),
                   'claude-runner', '--binding', str(home / relative)])
    rendered = render(registry, home, dict(id=target, settings=selected), existing_config,
                      _runtime=runtime)
    rendered['files'][relative] = json.dumps(binding, sort_keys=True, ensure_ascii=False) + '\n'
    rendered['helper_files'] = remote_helper_files()
    rendered['main_auth'] = dict(kind='claude', auth_source='manager_proxy', profile_id=target,
                                 expected_account_identity=identity)
    return rendered


def render_execution_role_for_host(registry, owner_home, profile, model, effort, *,
                                   host_id, remote_python, host_identity, remote_cli=None):
    selected = {**settings(profile.get('claude_settings')), 'model': model, 'reasoning_effort': effort}
    rendered = render_for_host(registry, owner_home, {**profile, 'claude_settings': selected},
                              host_id=host_id, remote_python=remote_python,
                              host_identity=host_identity, remote_cli=remote_cli)
    binding_name = 'claude-bindings/' + profile['id'] + '.json'
    binding = json.loads(rendered['files'][binding_name])
    binding['settings'] = settings(profile.get('claude_settings'))
    rendered['files'][binding_name] = json.dumps(binding, sort_keys=True, ensure_ascii=False) + '\n'
    config = tomllib.loads(rendered['files'].pop('config.toml'))
    provider_id = 'cc_claude_' + profile['id'].replace('-', '')
    provider = config['model_providers']['claude_code']
    provider['name'] = 'Claude · ' + profile.get('alias', 'registered account')
    return dict(provider_id=provider_id, provider=provider, files=rendered['files'],
                model='cc-' + model, effort=effort, catalog=config['model_catalog_json'],
                context_window=config['model_context_window'], helper_files=rendered['helper_files'],
                auth_metadata=rendered['main_auth'])


class ClaudeProfiles:
    def __init__(self, store):
        self.store = store

    def profile(self, profile_id):
        profile = self.store.profile(profile_id)
        if profile.get('auth_mode') != 'claude_code':
            raise ValueError('Claude 프로필을 선택하세요.')
        return profile

    def refresh(self, profile_id):
        from .claude_auth import ClaudeError, auth_status
        profile = self.profile(profile_id)
        try:
            observed = auth_status(profile['id'])
        except ClaudeError as error:
            observed = dict(logged_in=False, state=error.code, message=str(error))
        return self.record_status(profile_id, observed)

    def record_status(self, profile_id, observed):
        """Shared by manual refresh and runner preflight; never persist raw auth JSON."""
        self.profile(profile_id)
        allowed = ('logged_in', 'state', 'status', 'message', 'method', 'subscription_type',
                   'cli_version', 'masked_email', 'email')
        status = {key: observed[key] for key in allowed if key in observed}
        from .claude_auth import mask_email
        email = mask_email(status.pop('email', status.pop('masked_email', None)))
        if email:
            status['masked_email'] = email
        status.setdefault('state', status.get('status') or ('ready' if status.get('logged_in') else 'login_needed'))
        from .store import now
        status['observed_at'] = now()
        identity = observed.get('account_identity') if status.get('logged_in') is True else None
        if not isinstance(identity, str) or not re.fullmatch(r'[0-9a-f]{64}', identity):
            identity = None
        def update(data):
            current = self.store.profile(profile_id, data)
            if not identity or current.get('claude_account_identity') != identity:
                current['usage'] = {'provider': 'claude_code'}
            current.update(claude_status=status, claude_account_identity=identity)
        self.store.mutate(update)
        return status

    def login(self, profile_id):
        from .claude_auth import launch_login
        profile = self.profile(profile_id)
        result = launch_login(profile['id'])
        self.store.mutate(lambda data: self.store.profile(profile_id, data).update(
            claude_status=dict(logged_in=False, state='login_started'),
            claude_account_identity=None, usage={'provider': 'claude_code'}))
        return {**result, 'state': 'awaiting_user',
                'message': 'Claude 로그인 창에서 로그인을 마친 뒤 로그인 상태 확인을 눌러 주세요.'}

    def configure(self, profile_id, values):
        profile = self.profile(profile_id)
        values = dict(values)
        if 'effort' in values and 'reasoning_effort' not in values:
            values['reasoning_effort'] = values.pop('effort')
        selected = settings({**profile.get('claude_settings', {}), **values})
        def update(data):
            current = self.store.profile(profile_id, data)
            current['claude_settings'] = selected
            current['policy']['desired_revision'] = current['policy'].get('desired_revision', 0) + 1
            return current
        result = self.store.mutate(update)
        return dict(profile=result, state='saved', message='Claude 설정을 저장했습니다. 프로필을 다시 열면 적용됩니다.')
