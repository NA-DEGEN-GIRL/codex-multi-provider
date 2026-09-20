"""Install only manager-owned skill projections; serve updates over the SSH pipe."""
import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import sys
from uuid import UUID, uuid4


def directory(path):
    path = Path(path).absolute()
    for part in [*reversed(path.parents), path]:
        if part.is_symlink():
            raise ValueError('Skill bridge directory is a symbolic link.')
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def atomic(path, data, mode=0o600):
    if path.is_symlink():
        raise ValueError('Skill bridge file is a symbolic link.')
    directory(path.parent)
    temp = path.with_name(path.name + '.' + uuid4().hex + '.tmp')
    try:
        with temp.open('xb') as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        temp.chmod(mode)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def regular(path):
    if path.is_symlink() or (path.exists() and (not path.is_file() or path.stat().st_nlink != 1)):
        raise ValueError('Linked or non-regular skill bridge state is not allowed.')
    for parent in path.parents:
        if parent.is_symlink():
            raise ValueError('Linked skill bridge ancestors are not allowed.')


def install(payload, *, home=None):
    home = Path(home) if home else Path.home()
    root = directory(home / '.codex/workspace-skill-bridge')
    for name in ('owner.json', 'links.json', 'connection.json', 'client.py'):
        regular(root / name)
    owner = str(UUID(payload['owner']))
    marker = root / 'owner.json'
    if marker.exists() and json.loads(marker.read_text())['owner'] != owner:
        raise ValueError('Another workspace app owns this remote skill bridge.')
    atomic(marker, json.dumps({'owner': owner}).encode())
    files = payload['files']
    if not isinstance(files, dict) or len(files) > 1000:
        raise ValueError('Invalid skill projection inventory.')
    signature = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    release = directory(root / 'revisions' / signature)
    total = 0
    for name, encoded in files.items():
        relative = PurePosixPath(name)
        if (relative.is_absolute() or '..' in relative.parts or '\\' in name or ':' in name
                or not relative.parts or not name.endswith(('.md', '.py'))):
            raise ValueError('Invalid skill projection path.')
        data = base64.b64decode(encoded, validate=True); total += len(data)
        if total > 12 * 1024 * 1024:
            raise ValueError('Skill projection is too large.')
        target = release.joinpath(*relative.parts)
        regular(target)
        if not target.exists() or target.read_bytes() != data:
            atomic(target, data)
    client = base64.b64decode(payload['client'], validate=True)
    if len(client) > 128 * 1024:
        raise ValueError('Invalid bridge client size.')
    atomic(root / 'client.py', client)
    registry = root / 'links.json'
    previous = json.loads(registry.read_text()) if registry.exists() else {}
    if not isinstance(previous, dict):
        raise ValueError('Invalid previous skill projection inventory.')
    for name, target in previous.items():
        if (name not in ('3d-assets', 'game-audio') or not isinstance(target, str)
                or not Path(target).is_absolute() or not Path(target).is_relative_to(root / 'revisions')):
            raise ValueError('Previous skill projection is outside this bridge.')
    links, conflicts = {}, []
    skill_root = directory(home / '.agents/skills')
    for name in payload['enabled_skills']:
        if name not in ('3d-assets', 'game-audio'):
            raise ValueError('Unsupported projected skill.')
        target = release / name / '.agents/skills' / name
        if not (target / 'SKILL.md').is_file():
            raise ValueError('Projected skill is incomplete.')
        link = skill_root / name
        remote_native = home / '.codex/skills' / name
        owned = link.is_symlink() and str(link.readlink()) == previous.get(name)
        if ((link.exists() or link.is_symlink()) and not owned) or remote_native.exists() or remote_native.is_symlink():
            conflicts.append(name); continue
        replacement = link.with_name('.' + name + '-' + uuid4().hex)
        replacement.symlink_to(target, target_is_directory=True)
        os.replace(replacement, link)
        links[name] = str(target)
    for name, destination in previous.items():
        if name in links or name not in ('3d-assets', 'game-audio'):
            continue
        link = skill_root / name
        if link.is_symlink() and str(link.readlink()) == destination:
            link.unlink()
    atomic(registry, json.dumps(links).encode())
    config = dict(version=1, endpoint=payload['endpoint'], token=payload['token'],
                  client=str(root / 'client.py'), enabled_skills=sorted(links))
    atomic(root / 'connection.json', json.dumps(config).encode())
    return dict(installed=sorted(links), conflicts=conflicts, client=str(root / 'client.py'),
                connection=str(root / 'connection.json'), revision=signature)


def main():
    for line in sys.stdin:
        try:
            if len(line) > 24 * 1024 * 1024:
                raise ValueError('Bridge payload is too large.')
            result = install(json.loads(line))
            print(json.dumps(dict(ok=True, result=result)), flush=True)
        except (OSError, ValueError, KeyError, TypeError):
            # Never echo the payload: it contains a scoped connection credential.
            print(json.dumps(dict(ok=False, error='Remote skill bridge installation failed; existing native skills were preserved.')), flush=True)


if __name__ == '__main__':
    main()
