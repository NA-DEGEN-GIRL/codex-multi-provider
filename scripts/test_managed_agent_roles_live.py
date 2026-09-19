"""Check generated role discovery in the native runtime, without model requests."""
import json
from pathlib import Path
import re
import sys
from uuid import uuid4

from manager_core.providers import ProviderRegistry
from manager_core.runtime_build import resolve
from test_plugin_local_marketplace_headless import serve

ROOT = Path(__file__).resolve().parents[1]


def run():
    output = ROOT / 'artifacts/results' / ('agent-role-loading-' + uuid4().hex[:8])
    home = output / 'home'
    home.mkdir(parents=True)
    registry = ProviderRegistry(output)
    saved = registry.save(
        {'name': 'Synthetic external', 'base_url': 'https://provider.example/v1',
         'protocol': 'responses', 'adapter_id': 'native-responses', 'adapter_version': '1'},
        {'name': 'fixture-model', 'wire_model_id': 'fixture-model', 'reasoning_effort': 'low',
         'capabilities': {'verified': True, 'context_window': 32768, 'input_modalities': ['text'],
                          'supports_parallel_tool_calls': False, 'supports_reasoning_summaries': False,
                          'supported_reasoning_levels': ['low']}})
    generated = registry.render_for_host(home, True, [saved['model']['id']])['files']
    for name, content in generated.items():
        path = home / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')
    binary = resolve(ROOT)['runtime']

    def warnings():
        notifications = []
        serve(home, binary, [('config/read', {'includeLayers': True})], notifications=notifications)
        return [event['params'].get('summary', '') for event in notifications
                if event['method'] == 'configWarning'
                and 'malformed agent role' in event.get('params', {}).get('summary', '')]

    checks = {'declared_roles_valid': warnings() == []}
    # Settings and discovery may read the directory without its parent declarations.
    config = re.sub(r'(?ms)^\[agents\.[^\]]+\]\n.*?(?=^\[|\Z)', '', generated['config.toml'])
    (home / 'config.toml').write_text(config, encoding='utf-8')
    checks['standalone_roles_valid'] = warnings() == []
    roles = [home / name for name in generated if name.startswith('agents/')]
    for role in roles:
        role.write_text(re.sub(r'(?m)^description = .*\n', '', role.read_text(encoding='utf-8')),
                        encoding='utf-8')
    invalid = warnings()
    checks['old_format_reproduces_missing_description'] = len(invalid) == len(roles) == 5
    for name, content in generated.items():
        if name.startswith('agents/'):
            (home / name).write_text(content, encoding='utf-8')
    checks['repair_clears_native_warnings'] = warnings() == []
    report = {'passed': all(checks.values()), 'checks': checks, 'real_model_calls': 0,
              'user_homes_touched': False}
    (output / 'report.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report))
    print(output)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    sys.exit(run())
