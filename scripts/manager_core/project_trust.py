"""Share explicit project trust between this OS user's managed profiles.

Only project trust declarations cross profiles. Credentials, sandbox settings,
and remote-machine paths do not. A target declaration always takes precedence.
"""
import json
import ntpath
import os
from pathlib import Path
import posixpath
import tomllib

from .common import _atomic_write, config_lock


def path_key(path, *, windows):
    if not isinstance(path, str) or not path or any(ord(c) < 32 for c in path):
        return None
    if windows:
        if not ntpath.isabs(path) or not ntpath.splitdrive(path)[0]:
            return None
        value = ntpath.normcase(ntpath.normpath(path))
        return value.removeprefix('\\\\?\\')
    if not path.startswith('/') or '\\' in path:
        return None
    return posixpath.normpath(path)


def merge_projects(target, donors, *, windows):
    projects = target.get('projects', {})
    if not isinstance(projects, dict):
        return 0
    existing = {path_key(p, windows=windows) for p in projects}
    approved, denied = {}, set()
    for donor in donors:
        values = donor.get('projects', {})
        if not isinstance(values, dict):
            continue
        for path, entry in values.items():
            key = path_key(path, windows=windows)
            if key is None or not isinstance(entry, dict):
                continue
            if entry.get('trust_level') == 'trusted':
                approved.setdefault(key, path)
            elif entry.get('trust_level') == 'untrusted':
                denied.add(key)
    added = 0
    for key, path in approved.items():
        if key in existing or key in denied:
            continue
        projects[path] = {'trust_level': 'trusted'}
        added += 1
    if added:
        target['projects'] = projects
    return added


def sync(root, profile_id):
    from .store import Store
    store = Store(root)
    state = store.read()
    profile = store.profile(profile_id, state)
    if profile.get('view_only') or profile.get('removed_at'):
        return 0
    home = Path(profile['home']).resolve()
    expected = store.directory / 'profiles' / profile_id / 'codex'
    if home != expected.resolve():
        raise ValueError('Project trust target is outside its managed profile.')
    donors = []
    homes = [Path.home() / '.codex'] + [Path(p['home']) for p in state['profiles']
        if p['id'] != profile_id and not p.get('removed_at') and not p.get('view_only')]
    for other in homes:
        try:
            donors.append(tomllib.loads((other / 'config.toml').read_text(encoding='utf-8-sig')))
        except (OSError, ValueError):
            continue
    path = home / 'config.toml'
    home.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or path.resolve() != home / 'config.toml':
        raise ValueError('Project trust config must stay in its managed profile.')
    with config_lock(home):
        before = path.read_text(encoding='utf-8-sig') if path.exists() else ''
        target = tomllib.loads(before)
        old = set(target.get('projects', {}))
        count = merge_projects(target, donors, windows=os.name == 'nt')
        if not count:
            return 0
        # Append only new tables; never rewrite unrelated credentials or config.
        after = before.rstrip() + '\n\n' + ''.join(
            '[projects.' + json.dumps(p, ensure_ascii=False) + ']\ntrust_level = "trusted"\n\n'
            for p in target['projects'] if p not in old)
        if tomllib.loads(after) != target:
            raise ValueError('Project trust merge would change unrelated settings.')
        if (path.read_text(encoding='utf-8-sig') if path.exists() else '') != before:
            raise ValueError('Project config changed during trust synchronization.')
        _atomic_write(path, after)
        return count
