"""Install the pinned official Linux x64 workspace dependency runtime.

Run with native Linux Python 3.12 or newer. This installs dependencies only:
no account/configuration changes, service starts, restarts, or model calls.
Existing installations are never replaced. Use --archive for an offline copy
of the same verified archive; its pinned size and SHA-256 are still required.
"""
import argparse
import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import posixpath
import re
import shutil
import sys
import tarfile
import tempfile
import urllib.request

ROOT_NAME = 'codex-primary-runtime'
BUNDLE_VERSION = '26.923.10815'
ARCHIVE_URL = ('https://persistent.oaistatic.com/codex-primary-runtime/26.923.10815/'
               'codex-primary-runtime-linux-x64-26.923.10815.tar.xz')
ARCHIVE_BYTES = 383881884
ARCHIVE_SHA256 = '3a3f77f4f40811f9221ed79da598a376f02fad123a28a1acd8b0a0a0b4498af1'
USER_AGENT = 'codex-primary-runtime-installer'
PYTHON_BUILD_LINK = re.compile(r'/tmp/codex-primary-runtime-[A-Za-z0-9]+/python-download/python/(.+)\Z')


def require_platform():
    if sys.version_info < (3, 12):
        raise ValueError('Native Python 3.12 or newer is required.')
    if sys.platform != 'linux' or platform.machine().lower() not in ('x86_64', 'amd64'):
        raise ValueError('This pinned bundle requires native Linux x64.')


def checked_destination(target):
    target = Path(os.path.abspath(Path(target).expanduser()))
    if target == target.parent:
        raise ValueError('The filesystem root cannot be a runtime destination.')
    for path in [target, *target.parents]:
        if path.is_symlink():
            raise ValueError('Runtime destination and its ancestors must not be symbolic links.')
    return target


def ready(target):
    metadata = json.loads((target / 'runtime.json').read_text(encoding='utf-8'))
    if not isinstance(metadata, dict) or any(metadata.get(key) != value for key, value in (
            ('bundleVersion', BUNDLE_VERSION), ('targetPlatform', 'linux'), ('targetArch', 'x64'))):
        raise ValueError('Existing runtime does not match the pinned Linux bundle.')
    for name in ('dependencies/node/bin/node', 'dependencies/python/bin/python3'):
        path = target / name
        if not path.is_file() or not os.access(path, os.X_OK) or not path.resolve().is_relative_to(target):
            raise ValueError('Runtime executable is missing, non-executable, or outside the bundle.')
    for name in ('dependencies/node/node_modules', 'dependencies/python/lib',
                 'dependencies/bin/override', 'dependencies/bin/fallback', 'plugins/openai-primary-runtime'):
        path = target / name
        if not path.is_dir() or not path.resolve().is_relative_to(target):
            raise ValueError('Runtime dependency directory is missing or outside the bundle.')
    return metadata


def archive_path(name):
    path = PurePosixPath(name)
    if (not path.parts or path.is_absolute() or path.parts[0] != ROOT_NAME
            or '..' in path.parts or '\\' in name or ':' in name or '\x00' in name):
        raise ValueError('Archive member is outside the expected runtime root.')
    return path


def safe_member(member, destination):
    """Apply the tar data filter, repairing only known build-machine Python links."""
    archive_path(member.name)
    if member.issym() or member.islnk():
        link = member.linkname
        if link.startswith('/'):
            match = PYTHON_BUILD_LINK.fullmatch(link)
            if match is None:
                raise ValueError('Unknown absolute dependency link.')
            target = str(archive_path(ROOT_NAME + '/dependencies/python/' + match[1]))
            link = posixpath.relpath(target, posixpath.dirname(member.name)) if member.issym() else target
            member = member.replace(linkname=link)
        if '\\' in link or ':' in link or '\x00' in link:
            raise ValueError('Invalid dependency link.')
        target = posixpath.normpath(posixpath.join(posixpath.dirname(member.name), link)) if member.issym() else link
        archive_path(target)
    filtered = tarfile.data_filter(member, destination)
    stage = Path(destination).resolve()
    runtime_root = stage / ROOT_NAME
    destination_path = (stage / filtered.name).resolve()
    if not destination_path.is_relative_to(runtime_root):
        raise ValueError('Archive member resolves outside the runtime root.')
    if filtered.issym() or filtered.islnk():
        link_base = destination_path.parent if filtered.issym() else stage
        if not (link_base / filtered.linkname).resolve().is_relative_to(runtime_root):
            raise ValueError('Dependency link resolves outside the runtime root.')
    return filtered


def download(destination):
    request = urllib.request.Request(ARCHIVE_URL, headers={'User-Agent': USER_AGENT})
    size = 0
    with urllib.request.urlopen(request, timeout=60) as source, destination.open('xb') as output:
        while data := source.read(4 * 1024 * 1024):
            size += len(data)
            if size > ARCHIVE_BYTES:
                raise ValueError('Runtime download exceeds the pinned archive size.')
            output.write(data)
        output.flush()
        os.fsync(output.fileno())


def extract_verified(archive, stage):
    # Hash and extract using one open descriptor, preventing a path replacement
    # between validation and extraction from changing the archive being read.
    with archive.open('rb') as source:
        digest, size = hashlib.sha256(), 0
        while data := source.read(4 * 1024 * 1024):
            size += len(data)
            if size > ARCHIVE_BYTES:
                raise ValueError('Archive exceeds the pinned size.')
            digest.update(data)
        if size != ARCHIVE_BYTES or digest.hexdigest() != ARCHIVE_SHA256:
            raise ValueError('Archive size or SHA-256 does not match the pinned official bundle.')
        source.seek(0)
        with tarfile.open(fileobj=source, mode='r:*') as bundle:
            bundle.extractall(stage, filter=safe_member)


def publish_noreplace(source, destination):
    """Atomically publish on Linux without replacing even an empty target directory."""
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, 'renameat2', None)
    if rename is None:
        raise OSError(errno.ENOSYS, 'Atomic no-replace publication is unavailable.')
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(source), -100, os.fsencode(destination), 1) != 0:
        code = ctypes.get_errno()
        raise OSError(code, 'Runtime destination was not replaced: ' + os.strerror(code))


def install(target=None, archive=None):
    require_platform()
    target = checked_destination(target or Path.home() / '.cache/codex-runtimes' / ROOT_NAME)
    if os.path.lexists(target):
        ready(target)
        return dict(status='unchanged', target=str(target), bundleVersion=BUNDLE_VERSION)
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='.workspace-runtime-', dir=target.parent))
    try:
        if archive is None:
            archive = stage / 'bundle.tar.xz'
            download(archive)
        else:
            archive = Path(archive).expanduser().resolve(strict=True)
        extract_verified(archive, stage)
        extracted = stage / ROOT_NAME
        ready(extracted)
        receipt = dict(bundleVersion=BUNDLE_VERSION, archiveUrl=ARCHIVE_URL,
                       archiveSha256=ARCHIVE_SHA256, archiveSizeBytes=ARCHIVE_BYTES,
                       linkRepair='build-machine-python-links-only')
        with (extracted / '.linux-workspace-install.json').open('x', encoding='utf-8') as output:
            output.write(json.dumps(receipt, indent=2) + '\n')
        checked_destination(target)
        publish_noreplace(extracted, target)
        return dict(status='installed', target=str(target), bundleVersion=BUNDLE_VERSION,
                    archiveSha256=ARCHIVE_SHA256)
    finally:
        # Only the newly allocated staging directory is removed. Existing
        # destinations and an offline archive outside staging are never deleted.
        if stage.parent != target.parent or stage.is_symlink() or stage.resolve() != stage:
            raise ValueError('Runtime staging directory changed unexpectedly; cleanup refused.')
        shutil.rmtree(stage)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', type=Path)
    parser.add_argument('--archive', type=Path, help='Existing official archive for offline installation.')
    args = parser.parse_args()
    try:
        print(json.dumps(install(args.target, args.archive), indent=2))
    except (OSError, ValueError, tarfile.TarError) as error:
        parser.exit(1, 'Workspace runtime installation failed: ' + str(error) + '\n')


if __name__ == '__main__':
    main()
