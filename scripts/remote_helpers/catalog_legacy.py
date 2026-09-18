"""Discover existing Codex homes from metadata only; never load account auth."""
import hashlib
import json
import os
from pathlib import Path


def source(home, alias):
    home = str(Path(home).resolve())
    return dict(id='legacy:' + hashlib.sha256(home.encode()).hexdigest(), home=home, alias=alias[:160])


def discover(user_home, managed, *, environ=None):
    """Return read-only catalog homes. A changed home has a different identity."""
    environ = os.environ if environ is None else environ
    user_home = Path(user_home)
    seen = {str(Path(item['home']).resolve()) for item in managed}
    found, errors = [], []

    def add(home, alias):
        item = source(home, alias)
        if item['home'] not in seen:
            if len(found) + len(managed) >= 256:
                raise ValueError('Source inventory limit')
            seen.add(item['home'])
            found.append(item)

    try:
        stock = user_home / '.codex'
        try:
            stock.stat()
        except FileNotFoundError:
            pass
        else:
            add(stock, '기존 Codex')
    except (OSError, ValueError, RuntimeError):
        errors.append('stock')
    try:
        configured = environ.get('XDG_CONFIG_HOME')
        config_home = Path(configured).expanduser() if configured else user_home / '.config'
        if not config_home.is_absolute():
            raise ValueError('Relative registry root')
        registry = config_home / 'llm-usage/config.json'
        if registry.is_symlink():
            raise ValueError('Registry path changed')
        try:
            metadata = registry.stat()
        except FileNotFoundError:
            metadata = None
        if metadata is not None:
            if not registry.is_file() or metadata.st_size > 1024 * 1024:
                raise ValueError('Registry size limit')
            with registry.open(encoding='utf-8') as stream:
                content = stream.read(1024 * 1024 + 1)
            if len(content) > 1024 * 1024:
                raise ValueError('Registry size limit')
            value = json.loads(content)
            if type(value.get('schema_version')) is not int or value['schema_version'] not in (1, 2, 3):
                raise ValueError('Unsupported registry version')
            accounts = value['accounts']
            if not isinstance(accounts, list) or len(accounts) > 256:
                raise ValueError('Invalid account inventory')
            for account in accounts:
                if not isinstance(account, dict):
                    raise ValueError('Invalid account metadata')
                if account.get('provider') != 'codex':
                    continue
                home, alias = account.get('profile_dir'), account.get('alias')
                if (not isinstance(home, str) or not 1 <= len(home) <= 4096
                        or not isinstance(alias, str) or not 1 <= len(alias.strip()) <= 160
                        or any(ord(c) < 32 for c in home + alias)):
                    raise ValueError('Invalid Codex source metadata')
                # llm-usage writes resolved paths. Do not interpret relative
                # paths using the SSH process working directory.
                path = Path(home).expanduser()
                if not path.is_absolute():
                    raise ValueError('Relative Codex home')
                add(path, alias.strip())
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError):
        errors.append('llm_usage')
    return dict(sources=found, errors=errors)


def origins(sources):
    """Physical record roots handle llm-usage's shared sessions symlinks."""
    homes = {item['id']: Path(item['home']) for item in sources}
    roots = {}
    for sid, home in homes.items():
        for folder in ('sessions', 'archived_sessions'):
            logical = home / folder
            try:
                physical = logical.resolve()
            except (OSError, RuntimeError):
                continue
            # A symlink into a registered store belongs to that store. Do not
            # register a destination merely because a link points at it.
            owners = [key for key, value in homes.items() if physical == value / folder]
            if len(owners) == 1:
                roots[physical] = owners[0]
    return homes, roots


def origin_for(row, sid, homes, roots):
    if row.get('rollout_path'):
        # Listing metadata must not require opening (or even retaining) every
        # history file. Resolve directory aliases; selected reads validate files.
        path = Path(row['rollout_path']).resolve(strict=False)
        matches = {owner for root, owner in roots.items() if path.is_relative_to(root)}
        return next(iter(matches)) if len(matches) == 1 else None
    # Compact indexes do not have rollout paths. Only merge them when both
    # session directories explicitly point to the same registered source.
    owners = [roots.get((homes[sid] / folder).resolve()) for folder in ('sessions', 'archived_sessions')]
    return owners[0] if owners[0] is not None and owners[0] == owners[1] else sid
