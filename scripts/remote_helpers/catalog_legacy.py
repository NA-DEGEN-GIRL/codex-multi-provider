"""Discover existing Codex homes from metadata only; never load account auth."""
import hashlib
from pathlib import Path


def source(home, alias):
    home = str(Path(home).resolve())
    return dict(id='legacy:' + hashlib.sha256(home.encode()).hexdigest(), home=home, alias=alias[:160])


def discover(user_home, managed, *, environ=None):
    """Return read-only catalog homes. A changed home has a different identity.

    Only the stock ``$HOME/.codex`` is discovered; no account registry is read.
    ``environ`` is accepted for older callers and ignored.
    """
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
    return dict(sources=found, errors=errors)


def origins(sources):
    """Physical record roots handle symlinked sessions folders."""
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
