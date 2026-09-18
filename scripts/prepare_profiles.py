"""Generate isolated lab profiles; never writes the user's Codex home."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def quoted(value):
    return json.dumps(str(value), ensure_ascii=False)


def reasoning_effort_for_model(model):
    return 'max' if model == 'deepseek-flash' else 'low'


def enforce_flash_reasoning(model_info):
    if model_info.get('slug') == 'deepseek-flash':
        model_info['default_reasoning_level'] = 'max'
        model_info['supported_reasoning_levels'] = [
            {'effort': 'max', 'description': 'Required by the user: always use maximum reasoning for DeepSeek Flash.'}]


def prepare(overrides=None):
    provider_file = ROOT / 'profiles/provider.json'
    provider = json.loads(provider_file.read_text(encoding='utf-8-sig')) if provider_file.exists() else {
        'base_url': 'https://api.deepseek.com', 'model': 'deepseek-flash', 'protocol': 'responses'}
    provider.update(overrides or {})
    template = json.loads((ROOT / 'config/deepseek-catalog.json').read_text(encoding='utf-8'))
    external = template['models'][0]
    external['slug'] = provider['model']
    external['display_name'] = provider['model'] + ' (external lab)'
    enforce_flash_reasoning(external)
    roles = {'astra': 'gpt-6-astra', 'sol': 'gpt-5.6-sol', 'terra': 'gpt-5.6-terra', 'luna': 'gpt-5.6-luna'}
    for mode in ('upstream', 'runtime'):
        home = ROOT / 'profiles' / mode
        (home / 'agents').mkdir(parents=True, exist_ok=True)
        catalog = home / 'deepseek-models.json'
        catalog.write_text(json.dumps(template, indent=2, ensure_ascii=False), encoding='utf-8')
        common = f'''model = "gpt-6-astra"
model_reasoning_effort = "low"
web_search = "disabled"
approval_policy = "never"
sandbox_mode = "workspace-write"
sqlite_home = {quoted(home / 'sqlite')}
cli_auth_credentials_store = "file"
tool_output_token_limit = 3000
suppress_unstable_features_warning = true
'''
        if mode == 'runtime':
            common += 'subagent_model_provider_allowlist = ["deepseek_external"]\n'
        common += f'''
[windows]
sandbox = "unelevated"
[shell_environment_policy]
ignore_default_excludes = false
exclude = ["CODEX_EXTERNAL_DEEPSEEK_API_KEY", "CODEX_THREAD_ID", "CODEX_INTERNAL_ORIGINATOR_OVERRIDE"]
[features]
multi_agent = true
enable_request_compression = false
[features.multi_agent_v2]
enabled = true
tool_namespace = "collaboration"
expose_spawn_agent_model_overrides = true
hide_spawn_agent_metadata = false
wait_agent_enabled = true
[agents]
max_concurrent_threads_per_session = 4
'''
        for role, model in roles.items():
            common += f'\n[agents.{role}]\ndescription = {quoted("OpenAI / " + model + ". Choose according to the delegated task.")}\nconfig_file = "agents/{role}.toml"\n'
            (home / 'agents' / (role + '.toml')).write_text(f'model = "{model}"\n', encoding='utf-8')
        if mode == 'runtime':
            common += '''
[agents.deepseek]
description = "DeepSeek external provider. Use external_agents.spawn_agent with agent_type=deepseek for bounded coding, exploration and test tasks; fresh context only. DeepSeek Flash always uses max reasoning effort."
config_file = "agents/deepseek.toml"
'''
        common += f'''
[model_providers.deepseek_external]
name = "DeepSeek external lab"
base_url = {quoted(provider['base_url'])}
wire_api = "responses"
env_key = "CODEX_EXTERNAL_DEEPSEEK_API_KEY"
requires_openai_auth = false
supports_websockets = false
request_max_retries = 0
stream_max_retries = 0
stream_idle_timeout_ms = 60000
'''
        (home / 'config.toml').write_text(common, encoding='utf-8')
        (home / 'agents/deepseek.toml').write_text(
            f'model = {quoted(provider["model"])}\nmodel_provider = "deepseek_external"\n'
            f'model_catalog_json = {quoted(catalog)}\nmodel_reasoning_effort = {quoted(reasoning_effort_for_model(provider["model"]))}\n'
            'developer_instructions = "Work only on the assigned task inside the test workspace. Do not spawn more agents. Use the available tools, verify changes, and return a concise factual result."\n', encoding='utf-8')
    return provider


if __name__ == '__main__':
    print(json.dumps(prepare(), ensure_ascii=False))
