"""Native proof that the shared plugin mirror lists as installed with its skills.

Isolated fixtures only: a synthetic plugin bundle is installed in a temporary
profile home, the manager mirror is produced by ``manager_core.plugin_sync``,
and a real app-server reads the mirrored home. No credentials, network calls,
real plugin content or user installation is touched.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from manager_core.plugin_sync import MARKETPLACE, PluginSync
from manager_core.runtime_build import resolve as runtime_build
from manager_core.store import Store

PLUGIN = 'sample-plugin'
SKILL = 'sample-demo'
NATIVE = 'openai-curated-remote'
VERSION = '1.0.0'
SKILL_MD = ('---\nname: sample-demo\ndescription: Synthetic mirror fixture skill.\n---\n\n'
            '# Sample\n\nFixture body.\n')


def write_native(home):
    base = home / 'plugins/cache' / NATIVE / PLUGIN
    root = base / VERSION
    (root / '.codex-plugin').mkdir(parents=True)
    (root / '.codex-plugin/plugin.json').write_text(json.dumps({
        'name': PLUGIN, 'version': VERSION, 'description': 'Synthetic fixture plugin',
        'skills': './skills/', 'apps': './.app.json', 'hooks': './hooks/hooks.json'}) + '\n',
        encoding='utf-8')
    (root / '.app.json').write_text('{"apps":["synthetic"]}\n', encoding='utf-8')
    (root / 'hooks').mkdir()
    (root / 'hooks/hooks.json').write_text('{"hooks":[]}\n', encoding='utf-8')
    (root / 'skills' / SKILL).mkdir(parents=True)
    (root / 'skills' / SKILL / 'SKILL.md').write_text(SKILL_MD, encoding='utf-8')
    (base / '.codex-remote-plugin-install.json').write_text(
        json.dumps({'schema_version': 1, 'remote_plugin_id': 'plugin_synthetic'}) + '\n',
        encoding='utf-8')
    (home / 'config.toml').write_text(
        f'[marketplaces.{NATIVE}]\nsource_type = "local"\nsource = "fixture"\n\n'
        f'[plugins."{PLUGIN}@{NATIVE}"]\nenabled = true\n', encoding='utf-8')


def serve(home, binary, requests, *, timeout=60):
    env = {key: value for key, value in os.environ.items()
           if not key.upper().startswith(('CODEX_', 'OPENAI_', 'AZURE_OPENAI_', 'CHATGPT_'))
           and key.upper() != 'ELECTRON_RUN_AS_NODE'}
    env['CODEX_HOME'] = str(home)
    process = subprocess.Popen([str(binary), 'app-server', '--listen', 'stdio://'], cwd=home,
                               env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE,
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    messages = queue.Queue(maxsize=256)

    def receive():
        try:
            while True:
                line = process.stdout.readline(8 * 1024 * 1024 + 1)
                if not line:
                    break
                messages.put_nowait(json.loads(line))
        except (OSError, ValueError):
            pass
        messages.put_nowait(None)

    reader = threading.Thread(target=receive, daemon=True)
    reader.start()

    def send(value):
        process.stdin.write(json.dumps(value).encode('utf-8') + b'\n')
        process.stdin.flush()

    def request(identity, method, params):
        send({'id': identity, 'method': method, 'params': params})
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(method)
            value = messages.get(timeout=remaining)
            if value is None:
                error = (process.stderr.read() or b'').decode('utf-8', 'replace')[:800]
                raise RuntimeError('app-server exited: ' + error)
            if value.get('id') == identity and 'method' not in value:
                if 'error' in value:
                    raise RuntimeError(f'{method}: {value["error"]}')
                return value.get('result')
            if 'id' in value and 'method' in value:
                send({'id': value['id'], 'error': {'code': -32601, 'message': 'fixture'}})

    try:
        request(1, 'initialize', {'clientInfo': {'name': 'plugin_marketplace_proof', 'version': '1.0'},
                                  'capabilities': {'experimentalApi': True}})
        send({'method': 'initialized'})
        return {name: request(index + 2, name, params)
                for index, (name, params) in enumerate(requests)}
    finally:
        try:
            process.stdin.close()
        except OSError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        reader.join(timeout=1)
        process.stdout.close()
        process.stderr.close()


def installed_plugins(response):
    return {plugin.get('id'): plugin
            for marketplace in response.get('marketplaces', [])
            for plugin in marketplace.get('plugins', [])}


def skill_names(response):
    return [skill.get('name') for entry in response.get('data', [])
            for skill in entry.get('skills', [])]


def run(binary):
    binary = Path(binary).resolve(strict=True)
    output = ROOT / 'artifacts/results' / ('plugin-local-marketplace-' + uuid4().hex[:8])
    output.mkdir(parents=True)
    checks = {}
    report = dict(status='FAIL', gui_used=False, validation_kind='shared_plugin_local_marketplace',
                  user_homes_touched=False)
    try:
        store = Store(output / 'manager')
        peer = output / 'peer'
        original = output / 'original'
        profile = store.add_profile('02')
        peer.mkdir(parents=True)
        original.mkdir(parents=True)
        write_native(peer)

        def relink(data):
            for item in data['profiles']:
                item['home'] = str(peer)
        store.mutate(relink)
        result = PluginSync(store, source=original).reconcile(force=True)
        checks['reconcile_without_errors'] = not result['errors']
        shared = store.directory / 'shared-plugins' / MARKETPLACE
        mirror = original / 'plugins/cache' / MARKETPLACE / PLUGIN / VERSION
        checks['manager_marketplace_published'] = (shared / '.agents/plugins/marketplace.json').is_file()
        checks['mirror_bundle_complete'] = all((mirror / item).is_file() for item in
                                               ('.codex-plugin/plugin.json', '.app.json',
                                                'hooks/hooks.json', f'skills/{SKILL}/SKILL.md'))
        checks['remote_marker_not_copied'] = not (mirror.parent / '.codex-remote-plugin-install.json').exists()
        signal = json.loads((store.directory / 'record-signals/plugins.json').read_text(encoding='utf-8'))
        checks['signal_contract'] = signal.get('version') == 1 and bool(signal.get('revision'))
        config = json.loads(json.dumps(original.joinpath('config.toml').read_text(encoding='utf-8')))
        checks['mirror_config_entries'] = (f'[marketplaces.{MARKETPLACE}]' in config
                                           and f'[plugins."{PLUGIN}@{MARKETPLACE}"]' in config)

        responses = serve(original, binary, [('plugin/installed', {}),
                                             ('skills/list', {'cwds': [], 'forceReload': True})])
        entry = installed_plugins(responses['plugin/installed']).get(f'{PLUGIN}@{MARKETPLACE}')
        checks['native_lists_mirror_installed'] = bool(entry and entry.get('installed') is True
                                                       and entry.get('enabled') is True
                                                       and (entry.get('source') or {}).get('type') == 'local')
        checks['native_lists_mirror_skill'] = f'{PLUGIN}:{SKILL}' in skill_names(responses['skills/list'])

        shutil.rmtree(original / 'plugins/cache' / MARKETPLACE / PLUGIN)
        responses = serve(original, binary, [('plugin/installed', {})])
        entry = installed_plugins(responses['plugin/installed']).get(f'{PLUGIN}@{MARKETPLACE}')
        checks['cache_removal_hides_install'] = entry is None
        report['manager_result'] = result
        report['signal'] = signal
        report.update(status='PASS' if all(checks.values()) else 'FAIL', checks=checks)
    except Exception as error:  # pragma: no cover - reported, never hidden
        report.update(error=f'{type(error).__name__}: {error}', checks=checks)
    finally:
        with Path(binary).open('rb') as stream:
            report['runtime_sha256'] = hashlib.file_digest(stream, 'sha256').hexdigest()
        report['finished_at'] = datetime.now(timezone.utc).isoformat()
        path = output / 'report.json'
        path.write_text(json.dumps(report, indent=2), encoding='utf-8')
        print(json.dumps(dict(report=str(path), **report)))
    return report['status'] == 'PASS'


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path)
    args = parser.parse_args()
    runtime = args.runtime or Path(runtime_build(ROOT)['runtime'])
    raise SystemExit(0 if run(runtime) else 1)
