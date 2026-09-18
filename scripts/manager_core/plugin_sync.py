"""Share installed plugins through one manager-owned local marketplace.

Installed bundles are mirrored faithfully, including the ``.codex-plugin``
manifest, capabilities, app/connector declarations, skills, MCP declarations
and hooks. The receiving runtime keeps its own per-account authorization and
hook trust checks. Credentials, OAuth grants, remote installation markers,
``plugins/data`` and symlinks/junctions are never read or copied.

The mirror lives in its own marketplace name. The runtime's account-scoped
remote cleanup only removes entries under the six remote marketplace names
(``openai-curated-remote``, ``created-by-me-remote``, ``workspace-*``), so a
manager-owned namespace survives account reconciliation without a re-assert
loop. Ownership stays in ``plugin-sync.json``: publishing, updating and
unpublishing follow the home that installed the plugin, a removal is
remembered so a stale peer cannot resurrect it, and a peer that removes or
disables its own mirror is not overridden again.

Every write and delete is confined to ``<store>/shared-plugins/<MARKETPLACE>``
or ``<home>/plugins/cache/<MARKETPLACE>`` plus ``<home>/config.toml``; each
existing ancestor is checked for symlinks and junctions before a recursive
operation runs. A registry that cannot be read or validated is preserved and
reported instead of being reset.
"""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import threading
import time
import tomllib
from uuid import uuid4

from .common import _atomic_write, toml_value
from .store import atomic_json
from .updates import UpdateError, _lock_file, _unlock_file

MARKETPLACE = 'codex-manager-shared'
_STATE_VERSION = 1
_SIGNAL_VERSION = 1
_REMOTE_MARKETPLACES = (
    'openai-curated-remote',
    'created-by-me-remote',
    'workspace-directory',
    'workspace-shared-with-me',
    'workspace-shared-with-me-private',
    'workspace-shared-with-me-unlisted',
)
# App-bundled marketplaces materialize their own trees in every home and mutate
# them while the app runs. They are never promotion sources and never mirrored.
_BUNDLED_MARKETPLACES = ('openai-bundled', 'openai-primary-runtime')
_VERSION = re.compile(r'[A-Za-z0-9._-]{1,64}\Z')
_NAME = re.compile(r'[A-Za-z0-9._-]{1,120}\Z')
_PLUGIN_ID = re.compile(r'[A-Za-z0-9._-]{1,120}@[A-Za-z0-9._-]{1,120}\Z')
_GENERATION = re.compile(r'[A-Za-z0-9._-]{1,200}\Z')
_MAX_MARKETPLACES = 32
_MAX_PLUGINS = 256
_MAX_VERSIONS = 16
_MAX_MANIFEST_BYTES = 512 * 1024
_MAX_BUNDLE_ENTRIES = 20000
_MAX_BUNDLE_BYTES = 512 * 1024 * 1024
_MAX_TOMBSTONES = 256
_GENERATION_FILE = '.codex-manager-generation.json'


def _read(path):
    return path.read_text(encoding='utf-8-sig') if path.exists() else ''


def _long(path):
    """Return an extended-length path so deep mirror trees stay reachable.

    A manager-managed plugin mirror repeats a long profile home, the
    marketplace name, the plugin name and a staging directory before the
    plugin's own nested files. That alone can exceed the classic Windows
    MAX_PATH limit, which used to abort the whole home's synchronization.
    Only the concrete file operations use this form; confinement checks keep
    the ordinary path so two spellings never compare as different roots.
    """
    path = Path(path)
    if os.name != 'nt':
        return path
    value = os.path.abspath(str(path))
    if value.startswith('\\\\?\\'):
        return Path(value)
    if value.startswith('\\\\'):
        return Path('\\\\?\\UNC\\' + value[2:])
    return Path('\\\\?\\' + value)


def _key(path):
    return os.path.normcase(str(Path(path).resolve()))


def _linked(path):
    return path.is_symlink() or (hasattr(os.path, 'isjunction') and os.path.isjunction(path))


def _confined(base, target, label):
    """Resolve ``target`` inside ``base`` and reject links on every ancestor."""
    base = Path(base).resolve()
    target = Path(target)
    resolved = target.resolve() if os.path.lexists(target) \
        else target.parent.resolve(strict=False) / target.name
    if resolved != base and base not in resolved.parents:
        raise ValueError(f'{label} escapes the manager-owned directory.')
    cursor, steps = (target if os.path.lexists(target) else target.parent), 0
    while steps < 64:
        steps += 1
        if os.path.lexists(cursor):
            if _linked(cursor):
                raise ValueError(f'{label} is linked and is not managed.')
            if Path(cursor).resolve() == base:
                break
        if cursor.parent == cursor:
            break
        cursor = cursor.parent
    return resolved


def _home_config(home):
    """Return the confined ``config.toml`` path for a managed home."""
    home = Path(home)
    if not home.is_dir() or _linked(home):
        raise ValueError('Managed home must be a regular directory.')
    path = home / 'config.toml'
    if path.is_symlink():
        raise ValueError('Managed config must not be a link.')
    resolved = path.resolve() if os.path.lexists(path) else home.resolve() / 'config.toml'
    if home.resolve() != resolved.parent:
        raise ValueError('Managed config escapes its home.')
    return path


def _json_file(path, limit):
    if not path.is_file() or path.is_symlink() or path.stat().st_size > limit:
        return None
    try:
        value = json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _manifest(path):
    for candidate in (path / '.codex-plugin/plugin.json', path / 'plugin.json'):
        value = _json_file(candidate, _MAX_MANIFEST_BYTES)
        if value is not None:
            return value
    return None


def _version(value, fallback='local'):
    if isinstance(value, str) and _VERSION.fullmatch(value.strip()):
        return value.strip()
    return fallback


def _config_state(home):
    """Explicit plugin enablement plus declared marketplace names."""
    try:
        data = tomllib.loads(_read(Path(home) / 'config.toml'))
    except (OSError, ValueError):
        return {}, {}
    plugins, marketplaces = data.get('plugins'), data.get('marketplaces')
    plugins = plugins if isinstance(plugins, dict) else {}
    marketplaces = marketplaces if isinstance(marketplaces, dict) else {}
    states = {name: value.get('enabled', True) is not False
              for name, value in plugins.items()
              if isinstance(name, str) and isinstance(value, dict) and _PLUGIN_ID.fullmatch(name)}
    names = [name for name in marketplaces if isinstance(name, str) and _NAME.fullmatch(name)]
    return states, names


def _marketplace_names(home, configured):
    names = list(_REMOTE_MARKETPLACES) + [name for name in configured
                                          if name not in _REMOTE_MARKETPLACES]
    return list(dict.fromkeys(names))[:_MAX_MARKETPLACES]


def _promotable(marketplace):
    """Only account-installed remote marketplaces are mirrored."""
    return isinstance(marketplace, str) and marketplace in _REMOTE_MARKETPLACES


def inventory(home, marketplaces=None):
    """Bounded plugin inventory: marketplace, plugin and version names only.

    Bundle contents are never read, symlinks and junctions are never followed,
    and each home is capped by the module constants.
    """
    home = Path(home)
    states, configured = _config_state(home)
    names = _marketplace_names(home, configured if marketplaces is None else marketplaces)
    root = home / 'plugins/cache'
    found = {}
    for marketplace in names:
        directory = root / marketplace
        if not directory.is_dir() or _linked(directory):
            continue
        try:
            entries = sorted(directory.iterdir(), key=lambda item: item.name.casefold())[:_MAX_PLUGINS]
        except OSError:
            continue
        for entry in entries:
            name = entry.name
            if not entry.is_dir() or _linked(entry) or not _NAME.fullmatch(name):
                continue
            record = found.get(name)
            # A native installation always outranks a manager mirror.
            if record is not None and record['marketplace'] != MARKETPLACE:
                continue
            if record is not None and marketplace == MARKETPLACE:
                continue
            versions, newest = [], 0
            try:
                for version in sorted(entry.iterdir(), key=lambda item: item.name.casefold())[:_MAX_VERSIONS]:
                    if version.is_dir() and not _linked(version) and _VERSION.fullmatch(version.name):
                        versions.append(version.name)
                        newest = max(newest, version.stat().st_mtime_ns)
            except OSError:
                continue
            if not versions:
                continue
            marker = entry / '.codex-remote-plugin-install.json'
            manifest = None if marketplace == MARKETPLACE else _manifest(entry / versions[-1])
            manifest_time = 0
            if manifest is not None:
                manifest_path = entry / versions[-1] / '.codex-plugin/plugin.json'
                if manifest_path.is_file() and not manifest_path.is_symlink():
                    manifest_time = manifest_path.stat().st_mtime_ns
            generation = hashlib.sha256('|'.join((
                versions[-1], str(newest),
                str(marker.stat().st_mtime_ns) if marker.is_file() else '0',
                str(manifest_time))).encode('utf-8')).hexdigest()[:32]
            found[name] = dict(
                name=name, marketplace=marketplace, versions=versions,
                version=_version((manifest or {}).get('version')) if manifest else versions[-1],
                modified_ns=newest or entry.stat().st_mtime_ns,
                generation=generation,
                managed=marketplace == MARKETPLACE,
                native=marketplace != MARKETPLACE,
                promotable=_promotable(marketplace),
                marker=marker.is_file(),
                enabled=states.get(f'{name}@{marketplace}'),
            )
    return found


def signature(home, marketplaces=None):
    """Cheap change stamp: config file plus candidate marketplace directories."""
    home = Path(home)
    _, configured = _config_state(home)
    stamps = []
    for path in (home / 'config.toml', home / 'plugins/cache', home / 'plugins/cache' / MARKETPLACE):
        try:
            info = path.stat()
            stamps.append((str(path), info.st_mtime_ns, info.st_size))
        except OSError:
            stamps.append((str(path), None))
    for name in _marketplace_names(home, configured if marketplaces is None else marketplaces):
        path = home / 'plugins/cache' / name
        try:
            stamps.append((str(path), path.stat().st_mtime_ns))
        except OSError:
            stamps.append((str(path), None))
    return tuple(stamps)


def _owned_header(line, plugin_ids):
    """True when a TOML table header declares state this module owns."""
    text = line.strip()
    if not text.startswith('[') or text.startswith('[['):
        return False
    try:
        document = tomllib.loads(text + '\n')
    except tomllib.TOMLDecodeError:
        return False
    marketplaces, plugins = document.get('marketplaces'), document.get('plugins')
    if isinstance(marketplaces, dict) and MARKETPLACE in marketplaces and len(marketplaces) == 1:
        return True
    return isinstance(plugins, dict) and any(name in plugins for name in plugin_ids) \
        and len(plugins) == 1


def edit_config(text, *, marketplace_root, plugin_states):
    """Return ``config.toml`` text with only the manager-owned declarations changed.

    ``marketplace_root`` None removes the manager marketplace table.
    ``plugin_states`` maps a mirror plugin id to ``enabled`` or to None (remove).
    Every other statement, table, comment and ordering stays untouched.
    """
    plugin_ids = {name for name in plugin_states if name.endswith('@' + MARKETPLACE)}
    retained, statement, header, dropping_table = [], '', '', False
    for line in text.splitlines(keepends=True):
        if not statement and (not line.strip() or line.lstrip().startswith('#')):
            retained.append(line)
            continue
        statement += line
        try:
            tomllib.loads(statement)
        except tomllib.TOMLDecodeError:
            continue
        stripped = statement.lstrip()
        if stripped.startswith('['):
            header = stripped.split(']', 1)[0] + ']'
            dropping_table = (stripped.startswith('[marketplaces.') or stripped.startswith('[plugins.')) \
                and _owned_header(stripped, plugin_ids)
            dropping = dropping_table
        else:
            dropping = dropping_table or (
                (header == '[marketplaces]' and stripped.startswith(MARKETPLACE + ' '))
                or (header == '[plugins]' and any(
                    stripped.startswith(json.dumps(name) + ' ') for name in plugin_ids)))
        if not dropping:
            retained.append(statement)
        statement = ''
    if statement:
        raise ValueError('Plugin declarations could not be safely separated.')
    retained_text = ''.join(retained)
    if retained_text == text and not any(value is not None for value in plugin_states.values()):
        return text
    newline = '\r\n' if '\r\n' in text else '\n'
    additions = []
    if marketplace_root is not None:
        additions += ['[marketplaces.' + MARKETPLACE + ']',
                      'source_type = "local"',
                      'source = ' + toml_value(str(marketplace_root)), '']
    for name in sorted(plugin_states):
        enabled = plugin_states[name]
        if enabled is None or not name.endswith('@' + MARKETPLACE):
            continue
        additions += ['[plugins.' + json.dumps(name) + ']',
                      'enabled = ' + ('true' if enabled else 'false'), '']
    head = retained_text.rstrip()
    if additions:
        updated = ((head + newline + newline) if head else '') + newline.join(additions)
    else:
        updated = (head + newline) if head else ''
    expected = deepcopy(tomllib.loads(text))
    marketplaces = expected.setdefault('marketplaces', {})
    if marketplace_root is None:
        marketplaces.pop(MARKETPLACE, None)
    else:
        marketplaces[MARKETPLACE] = dict(source_type='local', source=str(marketplace_root))
    plugins = expected.setdefault('plugins', {})
    for name, enabled in plugin_states.items():
        if enabled is None:
            plugins.pop(name, None)
        else:
            plugins[name] = dict(enabled=bool(enabled))
    if not marketplaces:
        expected.pop('marketplaces', None)
    if not plugins:
        expected.pop('plugins', None)
    if tomllib.loads(updated) != expected:
        raise ValueError('Plugin declarations did not round-trip through config.toml.')
    return updated


def _copy_bundle(source, destination, base, *, attempts=2):
    """Copy an installed bundle into a confined destination without links.

    A running app can prune or rewrite its own marketplace trees while the
    copy runs. A vanished source is retried once from scratch instead of
    failing the whole pass; a bundle that is still broken afterwards raises.
    """
    _confined(base, destination, 'mirror bundle')
    blocked = ('.codex-remote-plugin-install.json',)
    if _linked(source) or not source.is_dir():
        raise ValueError('Plugin bundles must be regular directories.')
    last = None

    def copy(source_path, destination_path, budget):
        Path(_long(destination_path)).mkdir(parents=True, exist_ok=True)
        for item in sorted(Path(_long(source_path)).iterdir(), key=lambda value: value.name.casefold()):
            if _linked(item) or item.name in blocked:
                continue
            budget['entries'] += 1
            if budget['entries'] > _MAX_BUNDLE_ENTRIES:
                raise ValueError('Plugin bundle exceeds the entry budget.')
            target = Path(_long(Path(destination_path) / item.name))
            if item.is_dir():
                copy(item, target, budget)
            elif item.is_file():
                budget['bytes'] += item.stat().st_size
                if budget['bytes'] > _MAX_BUNDLE_BYTES:
                    raise ValueError('Plugin bundle exceeds the size budget.')
                shutil.copyfile(item, target)

    for _ in range(attempts):
        try:
            copy(source, destination, dict(entries=0, bytes=0))
            return destination
        except (FileNotFoundError, NotADirectoryError) as error:
            last = error
            _remove_directory(base, destination, 'mirror bundle')
            if not source.is_dir():
                break
        except OSError as error:
            last = error
            _remove_directory(base, destination, 'mirror bundle')
            break
    raise ValueError(f'Plugin bundle changed while copying: {last}')


def _publish_directory(base, staging, target, label):
    """Atomically publish ``staging`` as ``target`` inside ``base``."""
    _confined(base, staging, label)
    _confined(base, target, label)
    Path(_long(target).parent).mkdir(parents=True, exist_ok=True)
    trash = None
    if os.path.lexists(target):
        trash = target.with_name(target.name + '.manager-old-' + uuid4().hex[:8])
        _confined(base, trash, label)
        os.replace(_long(target), _long(trash))
    try:
        os.replace(_long(staging), _long(target))
    except OSError:
        if trash is not None and not os.path.lexists(target):
            os.replace(_long(trash), _long(target))
        raise
    finally:
        if trash is not None and os.path.lexists(trash):
            shutil.rmtree(_long(trash), ignore_errors=True)


def _remove_directory(base, target, label):
    """Recursively delete ``target`` after confinement checks."""
    _confined(base, target, label)
    if os.path.lexists(target):
        shutil.rmtree(_long(target), ignore_errors=True)


class PluginSync:
    """Keep manager-owned plugin mirrors consistent with their source home."""

    def __init__(self, store, source=None):
        self.store = store
        self.source = Path(source) if source is not None else None
        self.registry = store.directory / 'plugin-sync.json'
        self.lock_path = store.directory / 'plugin-sync.lock'
        self.shared_root = store.directory / 'shared-plugins' / MARKETPLACE
        self.signal = store.directory / 'record-signals' / 'plugins.json'
        self.lock = threading.RLock()
        self.stamp = None
        self.result = dict(applied=0, shared=0, removed=0, errors=[])
        self.stopping = threading.Event()
        self.worker = None

    def homes(self, state=None):
        # The canonical home participates only when the caller names it or the
        # manager already registered it as a local source. A store that never
        # declared it (for example a test fixture) must not touch it.
        state = self.store.read() if state is None else state
        homes = []
        original = self.source or next(
            (Path(item['home']) for item in state['sources']
             if item.get('host_id') == 'local'
             and Path(item['home']).resolve() == (Path.home() / '.codex').resolve()), None)
        if original is not None:
            homes.append(('original', original))
        for profile in state['profiles']:
            if profile.get('removed_at') or profile.get('view_only'):
                continue
            homes.append((profile.get('alias') or profile['id'], Path(profile['home'])))
        return list({_key(home): (alias, home) for alias, home in homes}.values())

    def _load(self):
        if not self.registry.exists():
            return dict(version=_STATE_VERSION, shared={}, removed={}, observations={}, optout={})
        value = _json_file(self.registry, _MAX_MANIFEST_BYTES)
        if value is None:
            raise ValueError('Shared plugin registry could not be read; the existing file was kept.')
        if (value.get('version') != _STATE_VERSION or not isinstance(value.get('shared'), dict)
                or not isinstance(value.get('removed'), dict)):
            raise ValueError('Shared plugin registry format is not recognized; the file was kept.')
        for name in ('observations', 'optout'):
            if not isinstance(value.get(name), dict):
                raise ValueError('Shared plugin registry format is not recognized; the file was kept.')
        for name, record in value['shared'].items():
            if not (isinstance(name, str) and _NAME.fullmatch(name) and isinstance(record, dict)
                    and isinstance(record.get('marketplace'), str)
                    and _NAME.fullmatch(record['marketplace'])
                    and record['marketplace'] != MARKETPLACE
                    and isinstance(record.get('version'), str)
                    and _VERSION.fullmatch(record['version'])
                    and isinstance(record.get('origin'), str)
                    and type(record.get('installed_at_ns')) is int
                    and (record.get('published_version') is None
                         or (isinstance(record['published_version'], str)
                             and _VERSION.fullmatch(record['published_version'])))
                    and (record.get('source_generation') is None
                         or (isinstance(record['source_generation'], str)
                             and _GENERATION.fullmatch(record['source_generation'])))):
                raise ValueError('Shared plugin registry holds an unsafe record; the file was kept.')
        for name, record in value['removed'].items():
            if not (isinstance(name, str) and _NAME.fullmatch(name) and isinstance(record, dict)
                    and type(record.get('removed_at_ns')) is int
                    and (record.get('version') is None
                         or (isinstance(record['version'], str)
                             and _VERSION.fullmatch(record['version'])))):
                raise ValueError('Shared plugin registry holds an unsafe tombstone; the file was kept.')
        return value

    def start(self):
        def watch():
            while not self.stopping.is_set():
                try:
                    self.reconcile()
                except (OSError, ValueError) as error:
                    self.result = dict(applied=0, shared=0, removed=0,
                                       errors=[dict(profile='shared plugins', message=str(error))])
                self.stopping.wait(2)
        self.worker = threading.Thread(target=watch, name='shared-plugins', daemon=True)
        self.worker.start()

    def shutdown(self):
        self.stopping.set()
        if self.worker:
            self.worker.join(timeout=3)

    def _shared_path(self, *parts):
        # Anchored at the manager directory so a link at `shared-plugins` or at
        # the marketplace directory cannot redirect the resolved root.
        return _confined(self.store.directory,
                         self.store.directory.joinpath('shared-plugins', MARKETPLACE, *parts),
                         'shared plugin path')

    def _managed_path(self, home, *parts):
        # Anchored at the home so a link at `plugins` or `plugins/cache` cannot
        # redirect the mirror outside the managed home.
        home = Path(home)
        if not home.is_dir() or _linked(home):
            raise ValueError('Managed home must be a regular directory.')
        return _confined(home, home.joinpath('plugins/cache', MARKETPLACE, *parts),
                         'mirror path')

    def _publish(self, record, donor, version, generation):
        """Copy the donor bundle into the manager marketplace source tree."""
        staging = self._shared_path('plugins.stage-' + uuid4().hex)
        published = self._shared_path('plugins', record['name'])
        try:
            _copy_bundle(donor / 'plugins/cache' / record['marketplace'] / record['name'] / version,
                         staging, self.store.directory)
            _publish_directory(self.store.directory, staging, published, 'published plugin')
        finally:
            if os.path.lexists(staging):
                shutil.rmtree(staging, ignore_errors=True)
        manifest_entry = self._shared_path('.agents/plugins/marketplace.json')
        document = _json_file(manifest_entry, _MAX_MANIFEST_BYTES) or dict(name=MARKETPLACE, plugins=[])
        plugins = [entry for entry in document.get('plugins', [])
                   if isinstance(entry, dict) and entry.get('name') != record['name']]
        plugins.append(dict(name=record['name'],
                            source=dict(source='local', path='./plugins/' + record['name']),
                            policy=dict(installation='AVAILABLE', authentication='ON_USE'),
                            category='Shared'))
        document['name'] = document.get('name') or MARKETPLACE
        document['plugins'] = sorted(plugins, key=lambda entry: str(entry.get('name', '')).casefold())
        manifest_entry.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(manifest_entry, document)
        return published

    def _unpublish(self, name):
        _remove_directory(self.store.directory, self._shared_path('plugins', name),
                          'published plugin')
        manifest_entry = self._shared_path('.agents/plugins/marketplace.json')
        document = _json_file(manifest_entry, _MAX_MANIFEST_BYTES)
        if document is None:
            return
        document['plugins'] = [entry for entry in document.get('plugins', [])
                               if not (isinstance(entry, dict) and entry.get('name') == name)]
        atomic_json(manifest_entry, document)

    def _apply_home(self, home, plan, states):
        """Materialize the plan for one home; returns the number of committed changes."""
        home = Path(home)
        if not home.is_dir() or _linked(home):
            raise ValueError('Managed home must be a regular directory.')
        mirror_root = home / 'plugins/cache' / MARKETPLACE
        _confined(home, mirror_root, 'mirror root')
        changes = 0
        entries = {}
        for name, record in plan.items():
            plugin_id = f'{name}@{MARKETPLACE}'
            if record.get('native'):
                entries[plugin_id] = None
            elif plugin_id in states:
                # A home's own toggle is never overridden by another client.
                entries[plugin_id] = states[plugin_id]
            else:
                entries[plugin_id] = bool(record.get('enabled', True))
        if mirror_root.is_dir():
            for item in mirror_root.iterdir():
                plugin_id = f'{item.name}@{MARKETPLACE}'
                if item.is_dir() and item.name not in plan and plugin_id not in entries:
                    entries[plugin_id] = None
        for name, record in plan.items():
            target = mirror_root / name / record['version']
            marker = mirror_root / name / _GENERATION_FILE
            applied = _json_file(marker, _MAX_MANIFEST_BYTES) or {}
            if record.get('native'):
                if os.path.lexists(mirror_root / name):
                    _remove_directory(home, mirror_root / name, 'mirror plugin')
                    changes += 1
                continue
            stale = [path for path in (mirror_root / name).iterdir()
                     if path.is_dir() and not _linked(path) and path.name != record['version']] \
                if (mirror_root / name).is_dir() else []
            if (not target.is_dir() or stale
                    or applied.get('generation') != record.get('source_generation')
                    or applied.get('version') != record['version']):
                staging = mirror_root / name / ('.stage-' + uuid4().hex)
                try:
                    _copy_bundle(self.shared_root / 'plugins' / name, staging, home)
                    _publish_directory(home, staging, target, 'mirror version')
                finally:
                    if os.path.lexists(staging):
                        shutil.rmtree(staging, ignore_errors=True)
                for path in stale:
                    _remove_directory(home, path, 'mirror version')
                _confined(home, marker, 'mirror generation')
                atomic_json(marker, dict(version=record['version'],
                                         generation=record.get('source_generation')))
                changes += 1
        for plugin_id, enabled in list(entries.items()):
            if enabled is None:
                _remove_directory(home, mirror_root / plugin_id.split('@', 1)[0], 'mirror plugin')
        for plugin_id in states:
            if plugin_id.endswith('@' + MARKETPLACE) and plugin_id not in entries:
                entries[plugin_id] = None
        config = _home_config(home)
        root = self.shared_root if any(value is not None for value in entries.values()) else None
        text = _read(config)
        updated = edit_config(text, marketplace_root=root, plugin_states=entries)
        if updated != text:
            if _read(config) != text:
                raise ValueError('Shared plugin config changed during preparation; retrying later.')
            _atomic_write(config, updated)
            changes += 1
        return changes

    def reconcile(self, force=False):
        """Run one propagation pass under the OS-owned plugin lock.

        The manager state lock is never held while bundles are copied: homes
        come from one short snapshot and every write happens under
        ``plugin-sync.lock``, so a slow pass cannot stall profile RPCs.
        """
        with self.lock:
            try:
                handle = _lock_file(self.lock_path)
            except UpdateError:
                # Busy is a skip, not a result: the watcher retries and a
                # profile open must never fail or wait on propagation.
                return dict(applied=0, shared=0, removed=0, busy=True,
                            errors=[dict(profile='shared plugins', code='busy',
                                         message='Another shared plugin pass is running.')])
            try:
                return self._reconcile(force)
            finally:
                _unlock_file(handle)

    def _reconcile(self, force=False):
        state = self.store.read()
        homes = self.homes(state)
        stamp = tuple((str(home), signature(home)) for _, home in homes)
        if not force and stamp == self.stamp:
            return self.result
        registry = self._load()
        baseline = json.dumps(registry, sort_keys=True, ensure_ascii=False)
        shared, removed = registry['shared'], registry['removed']
        observations = registry['observations']
        optout = registry['optout']
        inventories = {_key(home): inventory(home) for _, home in homes}
        states = {_key(home): _config_state(home)[0] for _, home in homes}
        now = time.time_ns()

        # A mirror removed by hand becomes a per-home opt-out.
        for _, home in homes:
            key = _key(home)
            previous = observations.get(key, {})
            for name in set(list(previous) + list(inventories[key])):
                found = inventories[key].get(name)
                mirror = bool(found and found['managed'])
                native = bool(found and found['native'])
                entry = states[key].get(f'{name}@{MARKETPLACE}')
                was = previous.get(name)
                if was and was.get('mirror') and not mirror and not native and entry is None:
                    optout.setdefault(key, {})[name] = now

        # Promote native installations; the existing owner keeps priority.
        for _, home in homes:
            key = _key(home)
            for name, found in inventories[key].items():
                if not found['native'] or not found['promotable']:
                    continue
                record = shared.get(name)
                if record is None:
                    if name in removed and found['modified_ns'] <= removed[name].get('removed_at_ns', 0):
                        continue
                    shared[name] = dict(name=name, marketplace=found['marketplace'],
                                        version=found['version'], origin=key,
                                        installed_at_ns=found['modified_ns'],
                                        source_generation=found['generation'],
                                        enabled=True if found['enabled'] is None else found['enabled'])
                    removed.pop(name, None)
                    for home_key in list(optout):
                        optout[home_key].pop(name, None)
                elif (record.get('origin') == key
                      and (found['modified_ns'] > record.get('installed_at_ns', 0)
                           or record.get('source_generation') != found['generation'])):
                    record.update(marketplace=found['marketplace'], version=found['version'],
                                  installed_at_ns=found['modified_ns'],
                                  source_generation=found['generation'],
                                  enabled=True if found['enabled'] is None else found['enabled'])

        # Removal follows the owner; a missing owner is retargeted or dropped.
        for name, record in list(shared.items()):
            if not _promotable(record.get('marketplace')):
                # Older state could point at an app-bundled marketplace.
                # Drop it without a tombstone so a real remote install of
                # the same plugin can still be promoted later.
                shared.pop(name)
                self._unpublish(name)
                continue
            owner = next((home for _, home in homes if _key(home) == record.get('origin')), None)
            if owner is None:
                natives = [home for _, home in homes
                           if inventories[_key(home)].get(name, {}).get('promotable')]
                if natives:
                    found = inventories[_key(natives[0])][name]
                    record.update(origin=_key(natives[0]), marketplace=found['marketplace'],
                                  version=found['version'], installed_at_ns=found['modified_ns'],
                                  source_generation=found['generation'])
                    continue
            else:
                found = inventories[_key(owner)].get(name)
                if found and found['native'] and found['promotable']:
                    record.update(enabled=True if found['enabled'] is None else found['enabled'])
                    continue
            shared.pop(name)
            removed[name] = dict(removed_at_ns=now, version=record.get('version'))
            for home_key in list(optout):
                optout[home_key].pop(name, None)
            self._unpublish(name)
        if len(removed) > _MAX_TOMBSTONES:
            ordered = sorted(removed, key=lambda item: removed[item].get('removed_at_ns', 0))
            for name in ordered[:len(removed) - _MAX_TOMBSTONES]:
                removed.pop(name, None)

        # Publish the current shared set and mirror it into every peer.
        desired = {}
        applied, errors = 0, []
        for name, record in shared.items():
            natives = [home for _, home in homes
                       if inventories[_key(home)].get(name, {}).get('promotable')]
            donor = next((home for home in natives if _key(home) == record['origin']),
                         natives[0] if natives else None)
            if donor is None:
                continue
            try:
                if (record.get('published_generation') != record.get('source_generation')
                        or not (self.shared_root / 'plugins' / name).is_dir()):
                    self._publish(record, donor, record['version'], record.get('source_generation'))
                    record['published_version'] = record['version']
                    record['published_generation'] = record.get('source_generation')
            except (OSError, ValueError) as error:
                errors.append(dict(profile='shared plugins', message=str(error)))
                continue
            desired[name] = record
        for alias, home in homes:
            key = _key(home)
            plan = {}
            for name, record in desired.items():
                found = inventories[key].get(name)
                if found and found['native']:
                    plan[name] = dict(record, native=True)
                elif optout.get(key, {}).get(name):
                    continue
                else:
                    plan[name] = dict(record, native=False)
            try:
                applied += self._apply_home(home, plan, states[key])
            except (OSError, ValueError) as error:
                errors.append(dict(profile=alias, message=str(error)))

        # Record what each home actually shows after this pass.
        for _, home in homes:
            key = _key(home)
            current, fresh = {}, inventory(home)
            fresh_states = _config_state(home)[0]
            for name in set(list(fresh) + list(observations.get(key, {}))):
                found = fresh.get(name)
                current[name] = dict(
                    mirror=bool(found and found['managed']),
                    native=bool(found and found['native']),
                    entry=fresh_states.get(f'{name}@{MARKETPLACE}'),
                    modified_ns=(found or {}).get('modified_ns', 0))
            observations[key] = current
        current = json.dumps(registry, sort_keys=True, ensure_ascii=False)
        if current != baseline:
            atomic_json(self.registry, registry)
        self.result = dict(applied=applied, shared=len(shared),
                           removed=len(removed), errors=errors)
        if applied or current != baseline:
            atomic_json(self.signal, dict(version=_SIGNAL_VERSION,
                                          revision=f'{time.time_ns()}-{uuid4().hex[:12]}'))
        self.stamp = tuple((str(home), signature(home)) for _, home in homes)
        if errors:
            self.stamp = None
        return self.result

    def status(self):
        with self.lock:
            registry = self._load()
            return dict(version=_STATE_VERSION, marketplace=MARKETPLACE,
                        root=str(self.shared_root), shared=registry['shared'],
                        removed=registry['removed'], optout=registry.get('optout', {}),
                        last=self.result)
