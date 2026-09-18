"""Crash-safe publication of program-only desktop copies and verified fallback."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import time
from uuid import uuid4

from .store import atomic_json


@contextmanager
def publication_lock(parent):
    parent.mkdir(parents=True, exist_ok=True)
    # The OS releases this lock even if the worker or computer stops abruptly.
    with (parent / 'publication.lock').open('a+b') as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b'0'); stream.flush()
        deadline = time.monotonic() + 120
        while True:
            try:
                stream.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError('Desktop preparation is still in progress.')
                time.sleep(.05)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def _hash(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _inventory(directory):
    return {p.relative_to(directory).as_posix(): dict(size=p.stat().st_size, modified=p.stat().st_mtime_ns)
            for p in directory.rglob('*') if p.is_file()}


def validated(directory, marker_name, identity=None):
    try:
        value = json.loads((directory / marker_name).read_text(encoding='utf8'))
        if identity is not None and value.get('source') != identity:
            return None
        if not value.get('files') or not value.get('hashes'):
            return None
        for name, expected in value['files'].items():
            path = directory / name
            if not path.resolve().is_relative_to(directory.resolve()) or path.is_symlink():
                return None
            info = path.stat()
            if expected != dict(size=info.st_size, modified=info.st_mtime_ns):
                return None
        for name, digest in value['hashes'].items():
            if _hash(directory / name) != digest:
                return None
        return value
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _fallback(parent, marker_name, identity):
    # A previous package is usable only with these exact manager adapters.
    ignored = {'source', 'version', 'size', 'modified'}
    adapter_identity = {k: v for k, v in identity.items() if k not in ignored}
    markers = sorted(parent.glob('*/' + marker_name), key=lambda p: p.stat().st_mtime_ns, reverse=True)
    for marker in markers:
        if '.staging-' in marker.parent.name or '.invalid-' in marker.parent.name:
            continue
        try:
            source = json.loads(marker.read_text(encoding='utf8'))['source']
            if {k: v for k, v in source.items() if k not in ignored} != adapter_identity:
                continue
        except (OSError, ValueError, KeyError):
            continue
        value = validated(marker.parent, marker_name)
        if value:
            return marker.parent, value
    return None


def publish(source, target, identity, marker_name, patch_archive, check_support, long_path):
    parent = target.parent
    with publication_lock(parent):
        ready = validated(target, marker_name, identity)
        if ready:
            return target, ready, None
        stage = target.with_name(target.name + '.staging-' + uuid4().hex)
        stage.mkdir()
        try:
            # Check compatibility before copying hundreds of MB on every click.
            try:
                check_support(source)
                (stage / 'resources').mkdir()
                patch = patch_archive(source / 'resources/app.asar', stage / 'resources/app.asar')
            except ValueError as error:
                fallback = _fallback(parent, marker_name, identity)
                if fallback:
                    return *fallback, str(error)
                raise
            shutil.copytree(long_path(source), long_path(stage), dirs_exist_ok=True,
                ignore=lambda path, names: ['app.asar'] if Path(path).name == 'resources' and 'app.asar' in names else [])
            original = (source / 'resources/app.asar').stat()
            if original.st_size != identity['size'] or original.st_mtime_ns != identity['modified']:
                raise OSError('Installed desktop changed during preparation; reopen the profile to retry.')
            archive = (stage / 'resources/app.asar').stat()
            value = dict(source=identity, patch=patch,
                archive=dict(size=archive.st_size, modified=archive.st_mtime_ns), files=_inventory(stage),
                hashes={name: _hash(stage / name) for name in ('ChatGPT.exe', 'chrome.dll', 'resources/app.asar')})
            atomic_json(stage / marker_name, value)
            if target.exists():
                # Preserve incomplete/corrupt copies for diagnosis; never merge them.
                target.rename(target.with_name(target.name + '.invalid-' + uuid4().hex))
            stage.rename(target)
            return target, value, None
        finally:
            # Only our private, unpublished program directory can be removed.
            if stage.exists() and stage.resolve().parent == parent.resolve() and not stage.is_symlink():
                shutil.rmtree(long_path(stage))
