"""Build a host-local canonical source allowlist for the managed Linux runtime."""
from pathlib import Path
from contextlib import contextmanager
import json
import os
from uuid import UUID

MAX_BYTES = 4 * 1024 * 1024
AUTHORITY_FIELDS = {'version','host_id','store_id','thread_id','owner_profile_id','epoch','revision'}


def _uuid(value):
    if not isinstance(value,str) or str(UUID(value)) != value:
        raise ValueError('canonical profile identifier required')
    return value


def _read(path, limit=32768):
    if path.is_symlink() or path.stat().st_nlink != 1 or path.stat().st_size > limit:
        raise ValueError('source metadata must be a bounded private file')
    value=json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value,dict):
        raise ValueError('source metadata object required')
    return value


@contextmanager
def _catalog_lock(base):
    path = base / 'catalog-sources.lock'
    if base.resolve(strict=True) != base or path.is_symlink():
        raise ValueError('catalog lock path')
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(descriptor, 'r+b') as stream:
        if os.fstat(stream.fileno()).st_nlink != 1:
            raise ValueError('catalog lock hardlink')
        if os.name == 'nt':
            import msvcrt
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            try:
                # Windows permits locking beyond EOF. Initialize only after
                # acquiring the byte lock: another process may already hold it
                # even while the newly created file is still empty.
                if stream.seek(0, os.SEEK_END) == 0:
                    stream.write(b'0'); stream.flush()
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)


def generate(profile, *, atomic, shared_catalog=False, legacy_discovery=False):
    profile = Path(profile)
    _uuid(profile.name)
    if profile.resolve(strict=True) != profile or profile.parent.name != 'profiles':
        raise ValueError('managed profile path required')
    if legacy_discovery and not shared_catalog:
        raise ValueError('legacy discovery requires a shared catalog')
    if shared_catalog:
        # Serialize enrollment with publication so simultaneous profile starts
        # cannot overwrite a newer source list with an earlier directory scan.
        with _catalog_lock(profile.parent.parent):
            return _generate(profile, atomic=atomic, shared_catalog=True, legacy_discovery=legacy_discovery)
    return _generate(profile, atomic=atomic, shared_catalog=False, legacy_discovery=False)


def _generate(profile, *, atomic, shared_catalog, legacy_discovery):
    profile=Path(profile)
    _uuid(profile.name)
    if profile.resolve(strict=True) != profile or profile.parent.name != 'profiles':
        raise ValueError('managed profile path required')
    home=profile/'codex'
    if not home.is_dir() or home.resolve(strict=True) != home:
        raise ValueError('managed source home required')
    marker={'host_id':'local','store_id':'manager:'+profile.name}
    marker_path=home/'managed-source.json'
    if marker_path.exists():
        if _read(marker_path) != marker:
            raise ValueError('source identity mismatch')
    else:
        atomic(marker_path,marker)
    sources=[]
    bindings={}
    candidates=sorted(profile.parent.iterdir())
    if len(candidates)>256:
        raise ValueError('managed profile limit exceeded')
    for candidate in candidates:
        source=candidate/'codex'
        marker_path=source/'managed-source.json'
        if not marker_path.exists():
            continue
        _uuid(candidate.name)
        if candidate.resolve(strict=True)!=candidate or source.resolve(strict=True)!=source:
            raise ValueError('source home symlink')
        expected={'host_id':'local','store_id':'manager:'+candidate.name}
        if _read(marker_path)!=expected:
            raise ValueError('source identity mismatch')
        sources.append(dict(hostId='local',sourceStoreId=expected['store_id'],codexHome=str(source)))
        authority=source/'managed-authority'
        if authority.is_symlink():
            raise ValueError('authority directory symlink')
        for path in sorted(authority.glob('*.json')):
            grant=_read(path)
            if (set(grant)!=AUTHORITY_FIELDS or grant['version']!=1 or grant['host_id']!='local'
                    or grant['store_id']!=expected['store_id'] or _uuid(grant['thread_id'])!=path.stem):
                raise ValueError('authority identity mismatch')
            _uuid(grant['owner_profile_id'])
            if any(type(grant[field]) is not int or not 1<=grant[field]<=2**53-1 for field in ('epoch','revision')):
                raise ValueError('authority version mismatch')
            if grant['thread_id'] in bindings:
                raise ValueError('ambiguous canonical thread')
            # Foreign-owner bindings authorize canonical reads only. The Rust
            # writer gate checks the current profile and durable grant again.
            bindings[grant['thread_id']]=dict(threadId=grant['thread_id'],hostId='local',
                sourceStoreId=grant['store_id'],ownerProfileId=grant['owner_profile_id'],
                ownershipEpoch=grant['epoch'],recordRevision=grant['revision'])
            if len(bindings)>4096:
                raise ValueError('managed binding limit exceeded')
    manifest=dict(version=1,profileId=profile.name,hostId='local',sources=sources,
                  bindings=sorted(bindings.values(),key=lambda item:item['threadId']))
    if len(json.dumps(manifest).encode('utf-8'))>MAX_BYTES:
        raise ValueError('managed source manifest limit exceeded')
    mixed = None
    if legacy_discovery:
        if __package__:
            from .catalog_legacy import discover
        else:
            from catalog_legacy import discover
        known = [dict(id=s['sourceStoreId'], home=s['codexHome']) for s in sources]
        discovery = discover(Path.home(), known)
        if discovery['errors']:
            raise ValueError('legacy source discovery unavailable; previous catalog preserved')
        legacy = [dict(hostId='local', sourceStoreId=s['id'], codexHome=s['home'])
                  for s in discovery['sources'] if Path(s['home']).is_dir()]
        # Older helpers keep publishing the V2 inventory. New runtimes read it
        # through this separate V3 descriptor, which older starts never replace.
        mixed = dict(version=3, hostId='local', sources=[], legacySources=legacy,
                     managedSourcesPath=str(profile.parent.parent / 'catalog-sources.json'))
        if len(json.dumps(mixed).encode('utf-8')) > MAX_BYTES:
            raise ValueError('mixed source catalog size limit')
    destination=profile/'managed-sources.json'
    atomic(destination,manifest)
    if shared_catalog:
        catalog = dict(version=2, hostId='local', sources=sources)
        atomic(profile.parent.parent / 'catalog-sources.json', catalog)
        if mixed is not None:
            atomic(profile.parent.parent / 'catalog-mixed-sources.json', mixed)
    return destination
