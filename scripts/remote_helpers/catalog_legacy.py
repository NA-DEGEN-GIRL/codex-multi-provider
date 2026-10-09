"""Discover existing Codex homes from metadata only; never load account auth."""
import hashlib
import json
from pathlib import Path

# The host's mixed source catalog, which managed_sources.py rewrites at each
# managed start. Before revision 121 that scan also enrolled the Codex homes
# listed in the llm-usage account registry.
ENROLLED_CATALOG = '.local/share/codex-control-center/catalog-mixed-sources.json'
MAX_ENROLLED_BYTES = 4 * 1024 * 1024


def source(home, alias):
    home = str(Path(home).resolve())
    return dict(id='legacy:' + hashlib.sha256(home.encode()).hexdigest(), home=home, alias=alias[:160])


def enrolled(path):
    """Homes an earlier managed start already published in the mixed catalog.

    A missing catalog enrolls nothing. A home that no longer exists, or now
    resolves elsewhere, is dropped, as an earlier scan would have dropped it.
    """
    path = Path(path)
    if path.is_symlink():
        raise ValueError('Enrolled catalog path changed')
    try:
        metadata = path.stat()
    except FileNotFoundError:
        return []
    if not path.is_file() or metadata.st_size > MAX_ENROLLED_BYTES:
        raise ValueError('Enrolled catalog size limit')
    with path.open(encoding='utf-8') as stream:
        content = stream.read(MAX_ENROLLED_BYTES + 1)
    if len(content) > MAX_ENROLLED_BYTES:
        raise ValueError('Enrolled catalog size limit')
    value = json.loads(content)
    entries = value['legacySources']
    if (value.get('version') != 3 or value.get('hostId') != 'local'
            or not isinstance(entries, list) or len(entries) > 256):
        raise ValueError('Invalid enrolled catalog')
    homes = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {'hostId', 'sourceStoreId', 'codexHome'}:
            raise ValueError('Invalid enrolled source')
        home = entry['codexHome']
        if (entry['hostId'] != 'local' or not isinstance(home, str) or not 1 <= len(home) <= 4096
                or any(ord(c) < 32 for c in home) or not Path(home).is_absolute()
                or entry['sourceStoreId'] != 'legacy:' + hashlib.sha256(home.encode()).hexdigest()):
            raise ValueError('Invalid enrolled source')
        if Path(home).is_dir() and str(Path(home).resolve()) == home:
            homes.append(Path(home))
    return homes


def discover(user_home, managed, *, enrolled_catalog=None):
    """Return read-only catalog homes. A changed home has a different identity.

    Finds the stock ``$HOME/.codex`` and keeps the homes an earlier managed
    start already enrolled in the host's mixed catalog, so helpers of
    different releases on one host publish the same homes. No account
    registry is read.
    """
    user_home = Path(user_home)
    enrolled_catalog = Path(enrolled_catalog) if enrolled_catalog is not None else user_home / ENROLLED_CATALOG
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
        for home in enrolled(enrolled_catalog):
            add(home, '기존 Codex' if home == user_home / '.codex' else '이전 연결 Codex · ' + home.name)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError):
        # The code names where these homes came from: the llm-usage registry
        # scan of releases before revision 121.
        errors.append('llm_usage')
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
