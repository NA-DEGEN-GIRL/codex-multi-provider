"""Publish portable plugin bundles into explicitly named managed SSH homes.

The caller supplies the existing plugin_sync.edit_config implementation. Account
state and native marketplace caches are never modified by this helper.
"""
import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
import tomllib
from uuid import UUID, uuid4

MARKETPLACE = 'codex-manager-shared'
NATIVE_MARKETS = ('openai-curated-remote', 'created-by-me-remote',
                  'workspace-directory', 'workspace-shared-with-me',
                  'workspace-shared-with-me-private', 'workspace-shared-with-me-unlisted')
NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,119}\Z')
VERSION = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z')
MAX_BYTES = 32 * 1024 * 1024
MAX_ENTRIES = 20000
FORBIDDEN = {'.exe', '.dll', '.pyd', '.so', '.dylib', '.cmd', '.bat', '.ps1'}
PRIVATE = {'auth.json', 'credentials.json', '.env', '.codex-remote-plugin-install.json'}


def confined(root, path):
    root, path = Path(root).absolute(), Path(path).absolute()
    if not path.is_relative_to(root) or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('Plugin path is outside its managed directory.')
    for ancestor in [path, *path.parents]:
        if ancestor.is_symlink() or (hasattr(os.path, 'isjunction') and os.path.isjunction(ancestor)):
            raise ValueError('Linked plugin paths are not supported.')
    if path.exists() and path.is_file() and path.stat().st_nlink != 1:
        raise ValueError('Hard-linked plugin files are not supported.')
    return path


def atomic(root, path, data):
    path = confined(root, path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name('.' + path.name + '.' + uuid4().hex)
    try:
        with temporary.open('xb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def decode_bundles(records):
    if not isinstance(records, list) or not 1 <= len(records) <= 256:
        raise ValueError('Invalid portable plugin inventory.')
    decoded, total, entries = {}, 0, 0
    for record in records:
        name, version = record['name'], record['version']
        if not NAME.fullmatch(name) or not VERSION.fullmatch(version) or name in decoded:
            raise ValueError('Invalid portable plugin identity.')
        files = {}
        for key, encoded in record['files'].items():
            path = PurePosixPath(key)
            if (not path.parts or path.is_absolute() or any(p in ('.', '..') for p in path.parts)
                    or '\\' in key or ':' in key or '\x00' in key or str(path) != key
                    or path.suffix.lower() in FORBIDDEN or path.name.lower() in PRIVATE):
                raise ValueError('Non-portable or private plugin file.')
            data = base64.b64decode(encoded, validate=True)
            total += len(data)
            entries += 1
            if total > MAX_BYTES or entries > MAX_ENTRIES or data.startswith((b'MZ', b'\x7fELF')):
                raise ValueError('Portable plugin content exceeds its contract.')
            files[key] = data
        manifest = json.loads(files['.codex-plugin/plugin.json'])
        if manifest.get('name') != name or manifest.get('version') != version:
            raise ValueError('Plugin manifest identity does not match inventory.')
        for key, data in files.items():
            if key.endswith('.json') and (key.startswith(('.codex-plugin/', 'hooks/')) or 'mcp' in key.lower()):
                declaration = json.loads(data)
                def strings(value):
                    if isinstance(value, dict):
                        for child in value.values():
                            yield from strings(child)
                    elif isinstance(value, list):
                        for child in value:
                            yield from strings(child)
                    elif isinstance(value, str):
                        yield value
                if any(re.search(r'(?i)(?:(?<![a-z0-9])[a-z]:[\\/]|\\\\|\b(?:powershell|pwsh|cmd)(?:\.exe)?\b)', value)
                       for value in strings(declaration)):
                    raise ValueError('Plugin declaration contains a Windows path or command.')
        digest = hashlib.sha256()
        for key, data in sorted(files.items()):
            digest.update(key.encode() + b'\0' + hashlib.sha256(data).digest())
        revision = digest.hexdigest()
        if revision != record['digest']:
            raise ValueError('Plugin content digest does not match inventory.')
        decoded[name] = dict(version=version, digest=revision, files=files,
                             enabled=record.get('enabled', True) is not False)
    return decoded


def materialize(root, target, files):
    """Create an immutable tree, verifying any existing release byte for byte."""
    confined(root, target)
    for key, data in files.items():
        destination = confined(root, target / key)
        if destination.exists():
            if not destination.is_file() or destination.read_bytes() != data:
                raise ValueError('Existing portable plugin content differs from its digest.')
        else:
            atomic(root, destination, data)
            if data.startswith(b'#!'):
                destination.chmod(0o700)


def install(payload, *, edit_config, home=None):
    home = Path(home) if home else Path.home()
    base = confined(home, home / '.local/share/codex-control-center')
    bundles = decode_bundles(payload['bundles'])
    profiles = payload['profiles']
    if not isinstance(profiles, list) or not 1 <= len(profiles) <= 64:
        raise ValueError('Invalid managed profile inventory.')
    homes = []
    for profile in profiles:
        identity = str(UUID(profile['id']))
        target = confined(base, base / 'profiles' / identity / 'codex')
        if str(target) != profile['home'] or not target.is_dir():
            raise ValueError('Remote profile home does not match its registered identity.')
        confined(base, target / 'config.toml')
        homes.append((identity, target))
    root = confined(base, base / 'shared-plugins' / MARKETPLACE)
    signature = hashlib.sha256(json.dumps({name: bundle['digest'] for name, bundle in bundles.items()},
                                         sort_keys=True).encode()).hexdigest()
    release = root / 'revisions' / signature
    marketplace = {'name': MARKETPLACE, 'plugins': []}
    for name, bundle in bundles.items():
        materialize(base, release / 'plugins' / name, bundle['files'])
        marketplace['plugins'].append(dict(name=name, source=dict(source='local', path='./plugins/' + name),
            policy=dict(installation='AVAILABLE', authentication='ON_USE'), category='Shared'))
    materialize(base, release, {'.agents/plugins/marketplace.json':
                              (json.dumps(marketplace, indent=2) + '\n').encode()})
    results = []
    for identity, target in homes:
        config_path = target / 'config.toml'
        original = config_path.read_text(encoding='utf-8-sig') if config_path.exists() else ''
        config = tomllib.loads(original)
        states, mirrored, native, disabled = {}, [], [], []
        for name, bundle in bundles.items():
            native_present = False
            for market in NATIVE_MARKETS:
                declaration = config.get('plugins', {}).get(name + '@' + market)
                cached = confined(target, target / 'plugins/cache' / market / name)
                if isinstance(declaration, dict) and declaration.get('enabled') is False:
                    native_present = True
                if cached.is_dir():
                    native_present |= any(confined(target, entry / '.codex-plugin/plugin.json').is_file()
                                          for entry in cached.iterdir() if entry.is_dir())
            key = name + '@' + MARKETPLACE
            if native_present:
                native.append(name)
                # Avoid loading an old manager mirror alongside the native bundle.
                if key in config.get('plugins', {}):
                    states[key] = False
                continue
            states[key] = config.get('plugins', {}).get(key, {}).get('enabled', bundle['enabled']) is not False
            if not states[key]:
                disabled.append(name)
            # The digest receipt detects same-version updates; the published
            # cache retains the plugin's original manifest and version.
            plugin = confined(target, target / 'plugins/cache' / MARKETPLACE / name)
            marker = confined(target, plugin / '.codex-manager-ssh.json')
            previous = json.loads(marker.read_text()) if marker.exists() else None
            if plugin.exists() and previous is None:
                raise ValueError('Existing manager plugin has no SSH publisher ownership receipt.')
            if previous and (previous.get('name') != name or not VERSION.fullmatch(previous.get('version', ''))):
                raise ValueError('Invalid SSH plugin ownership receipt.')
            version_path = plugin / bundle['version']
            if previous and previous.get('digest') == bundle['digest']:
                materialize(target, version_path, bundle['files'])
            else:
                staging = plugin / ('.stage-' + uuid4().hex)
                materialize(target, staging, bundle['files'])
                old = plugin / ('.previous-' + uuid4().hex)
                if version_path.exists():
                    os.replace(confined(target, version_path), old)
                try:
                    os.replace(staging, version_path)
                except OSError:
                    if old.exists():
                        os.replace(old, version_path)
                    raise
                if old.exists():
                    shutil.rmtree(confined(target, old))
                if previous and previous['version'] != bundle['version']:
                    stale = confined(target, plugin / previous['version'])
                    if stale.exists():
                        shutil.rmtree(stale)
                atomic(target, marker, json.dumps(dict(name=name, version=bundle['version'],
                                                      digest=bundle['digest'])).encode())
            mirrored.append(name)
        # Preserve all existing manager entries that are outside this explicit
        # publication; this command never uninstalls an unrelated plugin.
        updated = edit_config(original, marketplace_root=release, plugin_states=states)
        if updated != original:
            if (config_path.read_text(encoding='utf-8-sig') if config_path.exists() else '') != original:
                raise ValueError('Remote config changed during publication; retry explicitly.')
            atomic(target, config_path, updated.encode())
        results.append(dict(id=identity, mirrored=mirrored, native=native, disabled=disabled,
                            config_changed=updated != original))
    return dict(revision=signature, marketplace=str(release), profiles=results)


def main(edit_config):
    payload = sys.stdin.buffer.read(48 * 1024 * 1024 + 1)
    if len(payload) > 48 * 1024 * 1024:
        raise ValueError('Portable plugin request is too large.')
    print(json.dumps(install(json.loads(payload), edit_config=edit_config)), flush=True)
