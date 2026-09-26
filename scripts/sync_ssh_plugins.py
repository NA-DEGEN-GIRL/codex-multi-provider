"""Explicit one-shot publication of installed portable plugins to managed SSH homes.

Without --execute this prints a local plan and makes no SSH connection. This
publishes existing manager-shared bundles only; runtime/browser bundles, mutable
plugin data, credentials and OAuth state are outside this command's scope.
"""
import argparse
import base64
import gzip
import hashlib
import inspect
import json
from pathlib import Path
import shlex
import tempfile

from manager_core import plugin_sync
from manager_core.remote import RemoteManager
from manager_core.store import Store
from remote_helpers.plugin_install import decode_bundles


def bundle_payload(store):
    sync = plugin_sync.PluginSync(store)
    registry = sync._load()
    records = []
    with tempfile.TemporaryDirectory(prefix='codex-portable-plugins-') as temporary:
        staging = Path(temporary)
        for name, record in sorted(registry['shared'].items()):
            if not plugin_sync._promotable(record['marketplace']):
                continue
            source = sync.shared_root / 'plugins' / name
            if not source.is_dir():
                raise ValueError('An installed shared plugin has not been published locally.')
            plugin_sync._copy_bundle(source, staging / name, staging)
            files, digest = {}, hashlib.sha256()
            for path in sorted((staging / name).rglob('*')):
                if not path.is_file():
                    continue
                key = path.relative_to(staging / name).as_posix()
                data = path.read_bytes()
                files[key] = base64.b64encode(data).decode()
            for key in sorted(files):
                data = base64.b64decode(files[key])
                digest.update(key.encode() + b'\0' + hashlib.sha256(data).digest())
            records.append(dict(name=name, version=record['version'], digest=digest.hexdigest(),
                                enabled=record.get('enabled', True), files=files))
    decode_bundles(records)
    return records


def remote_source():
    # Reuse the tested editor verbatim, without importing manager Windows-only
    # process/lock modules into the remote interpreter.
    source = ('from copy import deepcopy\nimport json,tomllib\n'
              'MARKETPLACE="codex-manager-shared"\ntoml_value=json.dumps\n'
              + inspect.getsource(plugin_sync._owned_header) + '\n'
              + inspect.getsource(plugin_sync.edit_config) + '\n'
              + Path(__file__).with_name('remote_helpers').joinpath('plugin_install.py').read_text(encoding='utf-8')
              + '\nmain(edit_config)\n')
    encoded = base64.b64encode(gzip.compress(source.encode())).decode()
    return 'import base64,gzip;exec(gzip.decompress(base64.b64decode(' + repr(encoded) + ')))'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--host', required=True)
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    store = Store(args.root.resolve())
    profiles, python = [], None
    for profile in store.read()['profiles']:
        if profile.get('removed_at') or profile.get('view_only'):
            continue
        for binding in profile.get('remote_bindings', []):
            if binding.get('alias') == args.host:
                profiles.append(dict(id=profile['id'], home=binding['remote_profile_home']))
                python = binding['remote_python']
    if not profiles:
        raise ValueError('No registered managed profiles exist for this SSH host.')
    bundles = bundle_payload(store)
    if not args.execute:
        print(json.dumps(dict(host=args.host, profiles=profiles,
                              bundles=[{k: v for k, v in b.items() if k != 'files'} for b in bundles],
                              execute=False), indent=2))
        return
    remote = RemoteManager(args.root)
    remote._alias(args.host)
    response = remote._run(args.host, 'exec ' + shlex.quote(python) + ' -c ' + shlex.quote(remote_source()),
                           input=json.dumps(dict(profiles=profiles, bundles=bundles)).encode(), timeout=120)
    if response.returncode:
        raise RuntimeError('Remote portable plugin publication failed: ' + response.stderr.decode('utf-8', 'replace')[-2000:])
    print(json.dumps(json.loads(response.stdout), indent=2))


if __name__ == '__main__':
    main()
