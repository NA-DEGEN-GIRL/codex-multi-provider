"""Background SSH metadata cache. List reads never wait for SSH or change work."""
from .release_code import script_path
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shlex
import tempfile
import threading
import time

from .ssh_shim import validate_binding
from .store import identifier, now
from . import catalog_frames


BOOTSTRAP = '''import json,sys,types
value=json.load(sys.stdin)
for name in ('catalog','catalog_legacy','catalog_reader','catalog_frames'):
    module=types.ModuleType(name)
    sys.modules[name]=module
    exec(compile(value['modules'][name],name,'exec'),module.__dict__)
result=sys.modules['catalog_reader'].read(value['request'])
sys.modules['catalog_frames'].write(sys.stdout.buffer,result)
'''


def groups(state):
    result = {}
    for source in state['sources']:
        if not source.get('host_id', '').startswith('ssh:') or not source['id'].startswith('manager:'):
            continue
        try:
            profile_id = identifier(source['id'][8:])
            profile = next(p for p in state['profiles'] if p['id'] == profile_id)
            alias = source['host_id'][4:]
            bindings = [b for b in profile.get('remote_bindings', []) if b.get('alias') == alias and b.get('prepared') is True]
            if len(bindings) != 1:
                continue
            binding = validate_binding(bindings[0], profile_id)
            identity = bindings[0].get('host_identity', '')
            if not re.fullmatch('[0-9a-f]{64}', identity):
                continue
            expected = str(PurePosixPath(binding['remote_launcher']).parent / 'codex')
            if source['home'] != expected or bindings[0].get('remote_profile_home') != expected:
                continue
            group = result.setdefault(alias, dict(alias=alias, python=binding['remote_python'],
                host_identity=identity, sources=[], conflicted=False))
            if group['host_identity'] != identity or group['python'] != binding['remote_python']:
                group['conflicted'] = True
            group['sources'].append(dict(id=source['id'], home=source['home'], alias=source['alias']))
        except (KeyError, ValueError, RuntimeError, StopIteration, TypeError):
            continue
    for group in result.values():
        group['sources'].sort(key=lambda item: item['id'])
        scope = {**group, 'discover_legacy': True, 'sources': [{k: s[k] for k in ('id', 'home')} for s in group['sources']]}
        group['scope'] = hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()
    return result


class RemoteCatalog:
    def __init__(self, root, store, remote, *, reader=None, interval=5):
        self.root, self.store, self.remote = Path(root).resolve(), store, remote
        self.reader, self.interval = reader or self._read, interval
        self._lock = threading.RLock()
        self._cache, self._workers, self._next = {}, {}, {}
        self._stop = threading.Event()
        self._monitor = None

    def _path(self, alias):
        return self.root / 'work/control-center/catalog/ssh' / (hashlib.sha256(alias.encode()).hexdigest() + '.jsonl')

    def _read(self, group):
        alias = self.remote._alias(group['alias'])
        modules = {name: script_path(self.root, path).read_text(encoding='utf-8') for name, path in (
            ('catalog', 'scripts/manager_core/catalog.py'), ('catalog_legacy', 'scripts/remote_helpers/catalog_legacy.py'),
            ('catalog_reader', 'scripts/remote_helpers/catalog_reader.py'),
            ('catalog_frames', 'scripts/manager_core/catalog_frames.py'))}
        request = {k: group[k] for k in ('host_identity', 'sources')}
        request['discover_legacy'] = True
        payload = json.dumps(dict(modules=modules, request=request), ensure_ascii=False).encode('utf-8')
        if len(payload) > 262144:
            raise ValueError('SSH catalog request limit')
        with tempfile.TemporaryFile() as output:
            response = self.remote._run(alias, shlex.join([group['python'], '-c', BOOTSTRAP]),
                input=payload, timeout=30, stdout=output)
            if response.returncode:
                raise ValueError('SSH catalog unavailable')
            output.seek(0)
            return self._validate(catalog_frames.read(output), group)

    @staticmethod
    def _validate(value, group):
        sources = {s['id'] for s in group['sources']}
        if not isinstance(value, dict):
            raise ValueError('Invalid SSH catalog')
        discovered = value.get('discovered_sources', [])
        discovery_errors = value.get('discovery_errors', [])
        if (not isinstance(discovered, list) or len(discovered) + len(sources) > 256
                or not isinstance(discovery_errors, list)
                or any(e not in ('stock', 'llm_usage') for e in discovery_errors)):
            raise ValueError('Invalid SSH source discovery')
        homes = {s['home'] for s in group['sources']}
        inventory = []
        for item in discovered:
            if not isinstance(item, dict):
                raise ValueError('Invalid SSH source')
            home, alias = item.get('home'), item.get('alias')
            if (not isinstance(home, str) or not 1 <= len(home) <= 4096
                    or not PurePosixPath(home).is_absolute() or str(PurePosixPath(home)) != home
                    or '..' in PurePosixPath(home).parts or not isinstance(alias, str)
                    or not 1 <= len(alias.strip()) <= 160 or any(ord(c) < 32 for c in home + alias)):
                raise ValueError('Invalid SSH source metadata')
            sid = 'legacy:' + hashlib.sha256(home.encode()).hexdigest()
            if item.get('id') != sid or sid in sources or home in homes:
                raise ValueError('Invalid SSH source identity')
            sources.add(sid)
            homes.add(home)
            inventory.append(dict(id=sid, home=home, alias=alias.strip()))
        if (not isinstance(value.get('conversations'), list)
                or not isinstance(value.get('errors'), list)
                or any(not isinstance(e, str) or e not in sources for e in value['errors'])):
            raise ValueError('Invalid SSH catalog')
        rows = []
        for item in value['conversations']:
            if (not isinstance(item, dict) or item.get('source_store_id') not in sources or not isinstance(item.get('title'), str)
                    or len(item['title']) > 512 or item.get('cwd') is not None
                    and (not isinstance(item['cwd'], str) or len(item['cwd']) > 4096)
                    or not isinstance(item.get('updated_at'), (str, int, float, type(None)))):
                raise ValueError('Invalid SSH catalog entry')
            rows.append(dict(thread_id=identifier(item['thread_id']), source_store_id=item['source_store_id'],
                title=item['title'], cwd=item.get('cwd'), updated_at=item.get('updated_at'), archived=bool(item.get('archived'))))
        return dict(conversations=rows, errors=value['errors'], discovered_sources=inventory,
                    discovery_errors=discovery_errors, possibly_truncated=value.get('possibly_truncated') is True)

    def _cached(self, group):
        value = self._cache.get(group['alias'])
        if value is None:
            try:
                path = self._path(group['alias'])
                if path.is_symlink():
                    raise ValueError('Invalid metadata cache')
                if path.exists():
                    with path.open('rb') as stream:
                        value = catalog_frames.read(stream)
                else:
                    # Read the old bounded JSON cache until the first new refresh.
                    path = path.with_suffix('.json')
                    if path.is_symlink() or path.stat().st_size > 8 * 1024 * 1024:
                        raise ValueError('Invalid metadata cache')
                    value = json.loads(path.read_text(encoding='utf-8'))
                if value.get('scope') == group['scope']:
                    value.update(self._validate(value, group))
                    self._cache[group['alias']] = value
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                value = None
        return deepcopy(value) if value and value.get('scope') == group['scope'] else None

    def refresh(self, state=None):
        for alias, group in groups(state or self.store.read()).items():
            with self._lock:
                if (self._stop.is_set() or alias in self._workers or group['conflicted']
                        or time.monotonic() < self._next.get(alias, 0)):
                    continue
                worker = threading.Thread(target=self._refresh_host, args=(group,), daemon=True,
                                          name='remote-catalog-' + alias)
                self._workers[alias] = worker
                worker.start()

    def _refresh_host(self, group):
        alias = group['alias']
        failed = False
        try:
            result = self._validate(self.reader(group), group)
            with self._lock:
                previous = self._cached(group) or {}
            # Retain unavailable sources; a failed read must not erase their tasks.
            unavailable = set(result['errors'])
            if result['discovery_errors']:
                inventory = {s['id']: s for s in result['discovered_sources']}
                for source in previous.get('discovered_sources', []):
                    if source['id'] not in inventory:
                        inventory[source['id']] = source
                        unavailable.add(source['id'])
                result['discovered_sources'] = list(inventory.values())
            retained = [r for r in previous.get('conversations', []) if r['source_store_id'] in unavailable]
            unique = {(r['source_store_id'], r['thread_id']): r for r in [*retained, *result['conversations']]}
            incomplete = bool(result['errors'] or result['discovery_errors'])
            result.update(conversations=list(unique.values()), scope=group['scope'], observed_at=now(),
                          stale=incomplete, message='일부 SSH 기록은 이전 목록을 표시합니다.' if incomplete else '')
            self._validate(result, group)
            with self._lock:
                if not self._stop.is_set():
                    catalog_frames.atomic_write(self._path(alias), result)
                    self._cache[alias] = result
        except (OSError, ValueError, RuntimeError, KeyError, TypeError, AttributeError):
            failed = True
            with self._lock:
                previous = self._cached(group) or dict(scope=group['scope'], conversations=[], errors=[], observed_at=None)
                previous.update(stale=True, message='SSH에 연결하지 못해 이전 목록을 표시합니다.')
                self._cache[alias] = previous
        finally:
            with self._lock:
                self._workers.pop(alias, None)
                self._next[alias] = time.monotonic() + (max(30, self.interval) if failed else self.interval)

    def snapshot(self, state=None):
        rows, hosts = [], []
        with self._lock:
            for alias, group in groups(state or self.store.read()).items():
                cached = self._cached(group) or {}
                source_map = {s['id']: s for s in [*cached.get('discovered_sources', []), *group['sources']]}
                try:
                    age = (datetime.now(timezone.utc) - datetime.fromisoformat(cached['observed_at'])).total_seconds()
                    stale = cached.get('stale', True) or not 0 <= age <= max(15, self.interval * 3)
                except (KeyError, ValueError, TypeError):
                    stale = True
                if not group['conflicted']:
                    for row in cached.get('conversations', []):
                        source = source_map[row['source_store_id']]
                        rows.append({**row, 'host_id': 'ssh:' + alias, 'source_alias': source['alias'],
                            'source_home': source['home'], 'catalog_stale': stale})
                hosts.append(dict(host_id='ssh:' + alias, refreshing=alias in self._workers,
                    observed_at=cached.get('observed_at'), stale=stale,
                    possibly_truncated=cached.get('possibly_truncated', False),
                    message='SSH 호스트의 등록 정보를 확인해야 합니다.' if group['conflicted'] else
                            cached.get('message') or ('' if cached else 'SSH 대화 목록을 확인하고 있습니다.')))
        return dict(conversations=rows, hosts=hosts)

    def shortcut_source(self, host_id, source_id, thread_id):
        """Pin a cached source only when the user creates a task shortcut."""
        with self._lock:
            group = groups(self.store.read()).get(host_id[4:]) if host_id.startswith('ssh:') else None
            cached = self._cached(group) if group and not group['conflicted'] else None
            if cached and any(r['thread_id'] == thread_id and r['source_store_id'] == source_id
                              for r in cached['conversations']):
                source = next((s for s in cached.get('discovered_sources', []) if s['id'] == source_id), None)
                if source:
                    return {**source, 'host_id': host_id, 'host_identity': group['host_identity'], 'read_only': True}
        raise ValueError('공통 목록에서 확인한 SSH 대화를 선택하세요.')

    def start(self):
        with self._lock:
            if self._monitor and self._monitor.is_alive():
                return False
            self._stop.clear()
            def monitor():
                while not self._stop.is_set():
                    try:
                        self.refresh()
                    except (OSError, ValueError, RuntimeError):
                        pass
                    self._stop.wait(min(self.interval, 1))
            self._monitor = threading.Thread(target=monitor, daemon=True, name='remote-catalog-monitor')
            self._monitor.start()
            return True

    def stop(self):
        self._stop.set()
        if self._monitor:
            self._monitor.join(timeout=2)
