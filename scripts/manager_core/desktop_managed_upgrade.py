"""Audited, immutable UI compatibility upgrades of a known managed package.

This does not patch a newly installed integrity-enforced package or reconstruct
an official original. Only the pinned, previously published managed archive is
eligible; all adapters outside the reasoning UI must already be current.
"""
import hashlib
import json
from pathlib import Path
import shutil
import struct
from uuid import uuid4

from . import desktop_bundle as bundle
from . import desktop_publication as publication
from .store import atomic_json


_MARKER = 'manager-desktop.json'
_BASELINE_DIRECTORY = '26.917.9434.0-4b9ccd6b8c213650'
_BASELINE_MARKER_SHA256 = '728b6cb01ecd8a8b28d404dc78e14018f24986519e33e99e2b002e0110f7d92c'
_BASELINE_PATCH_SHA256 = 'f4809ef797c7fbb83a97bf90db6eeba9bc147152746c8489e692bcc03aca7606'
_BASELINE_REASONING_SHA256 = 'b65c8a9cc90f672f0c951abaec82b09bce436b427e1ced6a0b3e44c786780151'
_BASELINE_ARCHIVE_SHA256 = '8d32f2f75e8f2f3544fd062a7c6bbf4b94badd17458cf2802a21bc23db564593'


def _upgrade_plan_support(content):
    """Backport the official 26.930 personal-plan classification to 26.917.

    Unknown plans and workspace access checks stay unchanged. This only
    recognizes the additional personal plan already returned by the server;
    it does not synthesize an account, entitlement or server response.
    Exact anchors are intentional: another desktop requires its own audit.
    """
    replacements = (
        (b'e.PRO=`pro`,e.PROLITE=`prolite`',
         b'e.PRO=`pro`,e.PROMAX=`promax`,e.PROLITE=`prolite`'),
        (b'WIt=[pg.FREE,pg.GO,pg.PLUS,pg.PRO,pg.PROLITE,',
         b'WIt=[pg.FREE,pg.GO,pg.PLUS,pg.PRO,pg.PROMAX,pg.PROLITE,'),
        (b'Zzt=[`free`,`go`,`plus`,`prolite`,`pro`]',
         b'Zzt=[`free`,`go`,`plus`,`prolite`,`pro`,`promax`]'),
        (b'BRt=Es(`free.go.plus.pro.prolite.',
         b'BRt=Es(`free.go.plus.pro.promax.prolite.'),
    )
    for before, after in replacements:
        if content.count(before) != 1:
            raise ValueError('Managed desktop personal-plan upgrade anchor is not unique.')
        content = content.replace(before, after, 1)
    return content


def _baseline(source, current):
    if hashlib.sha256((source / _MARKER).read_bytes()).hexdigest() != _BASELINE_MARKER_SHA256:
        raise ValueError('Managed desktop upgrade source marker is not the audited baseline.')
    value = publication.validated(source, _MARKER)
    if not value or value['hashes'].get('resources/app.asar') != _BASELINE_ARCHIVE_SHA256:
        raise ValueError('Managed desktop upgrade source failed validation.')
    old = value['source']
    if (old.get('version') != '26.917.9434.0' or old.get('revision') != current['revision']
            or old.get('patch') != _BASELINE_PATCH_SHA256
            or old.get('adapters', {}).get('desktop_reasoning_ui.py') != _BASELINE_REASONING_SHA256):
        raise ValueError('Managed desktop upgrade source code identity is not supported.')
    expected = {name: digest for name, digest in current['adapters'].items()
                if name != 'desktop_managed_upgrade.py'}
    expected['desktop_reasoning_ui.py'] = _BASELINE_REASONING_SHA256
    if old['adapters'] != expected:
        raise ValueError('Managed desktop upgrade requires unchanged non-reasoning adapters.')
    # The package's existing Electron protection is checked without modifying it.
    bundle.check_archive_support(source)
    return value


def _patch_archive(source, destination):
    from .desktop_reasoning_ui import upgrade_managed, patch_composer
    with source.open('rb') as stream:
        header, base = bundle.read_header(stream)
        entries = list(bundle._entries(header))
        changed = []
        roles = set()
        for name, item in entries:
            role = ('picker' if name.startswith('webview/assets/app-initial') else
                    'composer' if name.startswith('webview/assets/app-primary') else None)
            if role is None or not name.endswith('.js'):
                continue
            if role in roles or not 0 < item['size'] <= 32 * 1024 * 1024:
                raise ValueError('Managed desktop reasoning upgrade entries are ambiguous.')
            roles.add(role)
            stream.seek(base + int(item['offset']))
            original = stream.read(item['size'])
            if len(original) != item['size']:
                raise ValueError('Truncated managed desktop reasoning entry.')
            updated = (upgrade_managed if role == 'picker' else patch_composer)(original)
            if role == 'picker':
                updated = _upgrade_plan_support(updated)
            if updated == original:
                raise ValueError('Managed desktop reasoning upgrade did not change its verified entry.')
            changed.append((name, item, original, updated))
        if roles != {'picker', 'composer'}:
            raise ValueError('Managed desktop reasoning upgrade requires both verified entries.')
        segments = []
        audit = []
        for name, item, original, updated in changed:
            segments.append((int(item['offset']), len(original), updated))
            block = item.get('integrity', {}).get('blockSize', 4 * 1024 * 1024)
            if type(block) is not int or not 0 < block <= 16 * 1024 * 1024:
                raise ValueError('Unsupported managed desktop integrity block size.')
            item['size'] = len(updated)
            item['integrity'] = dict(algorithm='SHA256', hash=hashlib.sha256(updated).hexdigest(), blockSize=block,
                blocks=[hashlib.sha256(updated[i:i + block]).hexdigest() for i in range(0, len(updated), block)])
            audit.append(dict(entry=name, before_sha256=hashlib.sha256(original).hexdigest(),
                after_sha256=hashlib.sha256(updated).hexdigest()))
        for _, item in entries:
            offset = int(item['offset'])
            item['offset'] = str(offset + sum(len(updated) - size for start, size, updated in segments if start < offset))
        document = json.dumps(header, ensure_ascii=False, separators=(',', ':')).encode()
        payload = struct.pack('<I', len(document)) + document + b'\0' * (-len(document) % 4)
        packed = struct.pack('<I', len(payload)) + payload
        with destination.open('wb') as output:
            output.write(struct.pack('<II', 4, len(packed)) + packed)
            stream.seek(base)
            position = 0
            for offset, size, updated in sorted(segments):
                remaining = offset - position
                if remaining < 0:
                    raise ValueError('Overlapping managed desktop archive entries.')
                while remaining:
                    data = stream.read(min(1024 * 1024, remaining))
                    if not data:
                        raise ValueError('Truncated managed desktop archive.')
                    output.write(data)
                    remaining -= len(data)
                output.write(updated)
                position = offset + size
                stream.seek(base + position)
            shutil.copyfileobj(stream, output, 1024 * 1024)
    return audit


def upgrade(root, source=None):
    """Publish one audited cache upgrade; ordinary prepare() may then fall back."""
    parent = Path(root).resolve() / 'artifacts/managed-desktop'
    source = Path(source) if source is not None else parent / _BASELINE_DIRECTORY
    if source.is_symlink() or source.resolve().parent != parent.resolve():
        raise ValueError('Managed desktop upgrade source must be a local published program directory.')
    current = bundle.adapter_identity()
    with publication.publication_lock(parent):
        baseline = _baseline(source, current)
        identity = {**baseline['source'], **current}
        key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:16]
        target = parent / (identity['version'] + '-' + key)
        if target == source:
            raise ValueError('Managed desktop upgrade must publish a separate directory.')
        ready = publication.validated(target, _MARKER, identity)
        if ready:
            return target, ready
        if target.exists():
            raise ValueError('Managed desktop upgrade target exists but did not validate.')
        stage = target.with_name(target.name + '.staging-' + uuid4().hex)
        stage.mkdir()
        try:
            # Copy only the program files recorded by the pinned publication marker.
            for name in baseline['files']:
                if name in (_MARKER, 'resources/app.asar'):
                    continue
                path = stage / name
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(bundle._long_path(source / name), bundle._long_path(path))
            (stage / 'resources').mkdir(exist_ok=True)
            audit = _patch_archive(source / 'resources/app.asar', stage / 'resources/app.asar')
            if _baseline(source, current) != baseline:
                raise OSError('Managed desktop upgrade source changed during publication.')
            info = (stage / 'resources/app.asar').stat()
            value = dict(source=identity, patch=baseline['patch'],
                archive=dict(size=info.st_size, modified=info.st_mtime_ns),
                files=publication._inventory(stage),
                hashes={name: publication._hash(stage / name) for name in baseline['hashes']},
                upgrade=dict(kind='managed-reasoning-and-plan-ui-v2', source_directory=str(source.resolve()),
                    source_marker_sha256=_BASELINE_MARKER_SHA256,
                    source_archive_sha256=_BASELINE_ARCHIVE_SHA256, entries=audit))
            for name, digest in baseline['hashes'].items():
                if name != 'resources/app.asar' and value['hashes'].get(name) != digest:
                    raise OSError('Managed desktop upgrade altered a protected program asset.')
            atomic_json(stage / _MARKER, value)
            if not publication.validated(stage, _MARKER, identity):
                raise OSError('Managed desktop upgrade failed final publication validation.')
            stage.rename(target)
            return target, value
        finally:
            if stage.exists() and stage.resolve().parent == parent.resolve() and not stage.is_symlink():
                shutil.rmtree(bundle._long_path(stage))
