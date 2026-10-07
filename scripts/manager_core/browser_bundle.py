"""Materialize the selected desktop's Browser code, never browser/account data.

The managed CLI can lag behind the desktop's bundled-plugin installer. The
desktop still gives Node REPL an exact, versioned browser-service path. Prepare
that version from the selected (or actually running) desktop, not the newest
system package or another account's cache. Retain older versions for live tasks.
"""
from collections import OrderedDict
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import threading
import time
from uuid import uuid4

from . import desktop_publication
from .desktop_publication import _cache_eligible, _change_time_ns, _content_stamp, _hash
from .updates import UpdateError, _lock_file, _unlock_file

_REQUIRED = ('.codex-plugin/plugin.json', 'scripts/browser-client.mjs',
             'scripts/browser-service.mjs', 'skills/control-in-app-browser/SKILL.md')
_MAX_FILES = 20000
_MAX_BYTES = 256 * 1024 * 1024
# One source tree plus one tree per profile home is several thousand files.
# They get their own digest cache instead of evicting the shared 64 entries.
_digests = OrderedDict()
_DIGEST_LIMIT = 8192
# (home, desktop executable, manifest digest) -> version, content stamps of
# the published files and (inode, mtime) of every target folder after a
# verified check, plus the files (with digests) and folders whose stamps were
# still too recent to prove a later change. An added, removed or renamed entry
# changes its folder's mtime, so a new link falls back to the full check.
_verified = OrderedDict()
_verified_lock = threading.Lock()
# Normalized home -> the request queued behind its running background job.
_jobs = {}
_jobs_lock = threading.Lock()


def _plain(path):
    """Reject symlinks and Windows junctions on all existing ancestors."""
    value = os.path.abspath(path)
    if os.name == 'nt' and not value.startswith('\\\\?\\'):
        value = '\\\\?\\UNC\\' + value[2:] if value.startswith('\\\\') else '\\\\?\\' + value
    path = Path(value)
    for item in (path, *path.parents):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('브라우저 구성요소 경로에 링크가 있어 변경하지 않았습니다.')
    return path


def _inside(base, path):
    base, path = _plain(base), _plain(path)
    if path == base or not path.is_relative_to(base):
        raise ValueError('브라우저 구성요소의 저장 경로를 확인하지 못했습니다.')
    return path


def _inventory(root, folders=None):
    root = _plain(root)
    result, pending, entries, size = {}, [root], 0, 0
    if folders is not None:
        info = root.lstat()
        folders[Path()] = (info.st_ino, info.st_mtime_ns)
    while pending:
        for path in pending.pop().iterdir():
            entries += 1
            if entries > _MAX_FILES:
                raise ValueError('브라우저 구성요소의 파일 수가 허용 범위를 넘었습니다.')
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise ValueError('브라우저 구성요소에 링크가 포함되어 있습니다.')
            if stat.S_ISDIR(info.st_mode):
                if folders is not None:
                    # Taken before the folder is listed, so a later entry changes it.
                    folders[path.relative_to(root)] = (info.st_ino, info.st_mtime_ns)
                pending.append(path)
            elif stat.S_ISREG(info.st_mode):
                size += info.st_size
                if size > _MAX_BYTES:
                    raise ValueError('브라우저 구성요소의 크기가 허용 범위를 넘었습니다.')
                result[path.relative_to(root)] = _hash(path, _digests, _DIGEST_LIMIT)
            else:
                raise ValueError('브라우저 구성요소에 일반 파일이 아닌 항목이 있습니다.')
    return result


def _changed(target, expected, folders=None):
    target = _plain(target)
    if not target.exists():
        return list(expected)
    if not target.is_dir():
        raise ValueError('브라우저 폴더 위치에 다른 종류의 항목이 있습니다.')
    # One bounded walk checks links and contents together. Rewalking every
    # ancestor for every dependency file would slow down profile selection.
    current = _inventory(target, folders)
    changed = [name for name, digest in expected.items() if current.get(name) != digest]
    for name in changed:
        path = target / name
        if path.exists() and not path.is_file():
            raise ValueError('브라우저 파일 위치에 다른 종류의 항목이 있습니다.')
    return changed


@contextmanager
def _locked(parent):
    deadline = time.monotonic() + 10
    while True:
        try:
            lock = _lock_file(_inside(parent, parent / '.manager-browser.lock'))
            break
        except UpdateError as error:
            if error.code != 'update_in_progress' or time.monotonic() >= deadline:
                raise RuntimeError('브라우저 파일을 준비하는 다른 요청이 있습니다. 잠시 후 다시 시도하세요.') from None
            time.sleep(.05)
    try:
        yield
    finally:
        _unlock_file(lock)


def _stamps(target, names, fresh=None):
    """Settled content stamps of the published files, or None if any is unusable.

    Size, mtime and NTFS ChangeTime of every file, not the directory mtime:
    an in-place edit deep in the tree does not change its parent's mtime.
    With `fresh`, a valid stamp too recent to prove a later edit is accepted
    and its name added there; the caller must prove that file by content.
    """
    stamps = {}
    try:
        for folder in {parent for name in names for parent in Path(name).parents if parent.parts}:
            info = (target / folder).lstat()
            if not stat.S_ISDIR(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                return None
        for name in names:
            path = target / name
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                return None
            with path.open('rb') as stream:
                stamp = _content_stamp(stream)
            if not _cache_eligible(stamp):
                if fresh is None or _change_time_ns(stamp) is None:
                    return None  # Unavailable or too recent to prove a later edit.
                fresh.add(name)
            stamps[name] = stamp
    except OSError:
        return None
    return stamps


def _same_folders(target, folders):
    try:
        for name, stamp in folders.items():
            info = (target / name).lstat()
            if (not stat.S_ISDIR(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400
                    or (info.st_ino, info.st_mtime_ns) != stamp):
                return False
    except OSError:
        return False
    return True


def _listed(target, name, folders):
    """A folder holds only regular files and folders the remembered check walked."""
    for count, path in enumerate((target / name).iterdir()):
        info = path.lstat()
        if (count >= _MAX_FILES or stat.S_ISLNK(info.st_mode)
                or getattr(info, 'st_file_attributes', 0) & 0x400):
            return False
        if stat.S_ISDIR(info.st_mode):
            if path.relative_to(target) not in folders:
                return False
        elif not stat.S_ISREG(info.st_mode):
            return False
    return True


def _settled(folders):
    """A folder mtime proves later changes only once it is a whole second old."""
    limit = desktop_publication._now_ns() - desktop_publication._HASH_MIN_AGE_NS
    return bool(folders) and all(mtime <= limit for _, mtime in folders.values())


def _memo_key(home, executable, manifest):
    try:
        info = Path(executable).stat()
    except OSError:
        return None
    return (os.path.normcase(str(home)), os.path.normcase(str(Path(executable).absolute())),
            info.st_ino, info.st_size, info.st_mtime_ns, hashlib.sha256(manifest).hexdigest())


def _remembered(key, target):
    with _verified_lock:
        entry = _verified.get(key)
    if entry is None:
        return None
    version, stamps, folders, files, listed = entry
    now, fresh = desktop_publication._now_ns(), set()
    try:
        # A file or folder changed just before it was remembered is proven by
        # its content or listing again, until its stamp is old enough that a
        # later change would alter it. Read first, then compare the stamps.
        rechecked = (all(_listed(target, name, folders) for name in listed)
                     and all(_hash(target / name, _digests, _DIGEST_LIMIT) == digest
                             for name, digest in files.items()))
    except (OSError, ValueError):
        rechecked = False
    if (not rechecked or not _same_folders(target, folders)
            or _stamps(target, stamps, fresh) != stamps or not fresh <= files.keys()):
        with _verified_lock:
            if _verified.get(key) is entry:
                del _verified[key]
        return None
    if files or listed:
        limit = now - desktop_publication._HASH_MIN_AGE_NS
        files = {name: digest for name, digest in files.items()
                 if not _cache_eligible(stamps[name], now_ns=now)}
        listed = frozenset(name for name in listed if folders[name][1] > limit)
        with _verified_lock:
            if _verified.get(key) is entry:
                _verified[key] = (version, stamps, folders, files, listed)
    return dict(state='ready', version=version, files=len(stamps), changed_files=0)


def _remember(key, version, stamps, folders, files=None, listed=frozenset()):
    if key is None:
        return
    with _verified_lock:
        _verified[key] = (version, stamps, folders, files or {}, listed)
        _verified.move_to_end(key)
        while len(_verified) > 64:
            _verified.popitem(last=False)


def _source(executable):
    return _plain(Path(executable).absolute().parent / 'resources/plugins/openai-bundled/plugins/browser')


def ensure(home, executable):
    """Verify/publish one exact Browser version; no process or config changes.

    New versions appear only after complete staging and hash verification.
    An incomplete existing version is repaired with atomic replacements of the
    staged changed files, so its good files remain readable even if
    preparation fails or is interrupted. A verified version is remembered for
    this service while the desktop executable, the manifest, every published
    file's stamp and every folder's entries are unchanged.
    """
    source = _source(executable)
    if not source.exists():
        return dict(state='not_bundled')  # Desktop editions without Browser.
    manifest = source / '.codex-plugin/plugin.json'
    _plain(manifest)
    if not manifest.is_file() or manifest.stat().st_size > 512 * 1024:
        raise ValueError('앱에 포함된 브라우저 구성요소 정보를 확인하지 못했습니다.')
    raw = manifest.read_bytes()
    value = json.loads(raw.decode('utf-8-sig'))
    version = value.get('version') if isinstance(value, dict) else None
    if (not isinstance(value, dict) or value.get('name') != 'browser' or not isinstance(version, str)
            or not re.fullmatch(r'[0-9]+(?:\.[0-9]+){1,4}', version)):
        raise ValueError('앱에 포함된 브라우저 버전 정보가 올바르지 않습니다.')
    home = _plain(home)
    key = _memo_key(home, executable, raw)
    if key is not None:
        ready = _remembered(key, _inside(home, home / 'plugins/cache/openai-bundled/browser' / version))
        if ready:
            return ready
    expected = _inventory(source)
    if any(Path(name) not in expected for name in _REQUIRED):
        raise ValueError('앱에 포함된 브라우저 필수 파일이 누락되었습니다.')
    if expected[Path('.codex-plugin/plugin.json')] != hashlib.sha256(raw).hexdigest():
        raise OSError('브라우저 구성요소를 확인하는 동안 앱 파일이 변경되었습니다.')
    parent = _inside(home, home / 'plugins/cache/openai-bundled/browser')
    parent.mkdir(parents=True, exist_ok=True)
    target = _inside(parent, parent / version)
    with _locked(parent):
        # Stamps taken before and after one verification prove that the
        # verified bytes are the stamped ones.
        before, folders, published = _stamps(target, expected), {}, {}
        result = _publish(source, parent, target, version, expected, folders, published)
        if (result['state'] == 'ready' and before is not None and _settled(folders)
                and _stamps(target, expected) == before):
            _remember(key, version, before, folders)
        elif published:
            # A repair is remembered with the stamps of its final check, so the
            # next call does not repeat it. What it changed just now is proven
            # again by content and listing until its stamps have settled.
            limit = desktop_publication._now_ns() - desktop_publication._HASH_MIN_AGE_NS
            _remember(key, version, published['stamps'], published['folders'],
                      {name: expected[name] for name in published['fresh']},
                      frozenset(name for name, (_, mtime) in published['folders'].items() if mtime > limit))
        return result


def _publish(source, parent, target, version, expected, folders=None, published=None):
    # Staged in the profile's plugin cache (same volume), never inside the
    # Browser plugin folder: every folder there is listed as a version.
    cache = parent.parent.parent
    staging = _inside(cache, cache / ('.manager-stage-' + uuid4().hex))
    try:
        changed = _changed(target, expected, folders)
        if not changed:
            return dict(state='ready', version=version, files=len(expected), changed_files=0)
        # A new version is staged complete; a repair stages only what it replaces.
        staging.mkdir()
        for name in changed:
            destination = staging / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / name, destination)
        staged = set(changed)
        if (_inventory(staging) != {name: expected[name] for name in changed}
                or _inventory(source) != expected):
            raise OSError('브라우저 구성요소가 복사 중 변경되어 적용하지 않았습니다.')
        if not target.exists():
            if len(staged) != len(expected):
                raise OSError('브라우저 구성요소가 준비 중 변경되었습니다. 다시 시도하세요.')
            _inside(parent, target)
            staging.rename(target)
            state = 'prepared'
        else:
            # Validate all destinations before the first change. Leave unowned
            # extra files alone; only package-declared assets may be replaced.
            changed = _changed(target, expected)
            if not staged.issuperset(changed):
                raise OSError('브라우저 구성요소가 준비 중 변경되었습니다. 다시 시도하세요.')
            for name in changed:
                destination = _inside(parent, target / name)
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(staging / name, destination)
            state = 'repaired'
        # A new version's files are all fresh, so only a repair hands back the
        # stamps taken around this final check to be remembered.
        fresh = set()
        stamps = _stamps(target, expected, fresh) if published is not None and state == 'repaired' else None
        checked = {}
        if _changed(target, expected, checked):
            raise OSError('브라우저 구성요소의 복사 결과를 확인하지 못했습니다.')
        if stamps is not None and _stamps(target, expected, set()) == stamps:
            published.update(stamps=stamps, folders=checked, fresh=fresh)
        return dict(state=state, version=version, files=len(expected), changed_files=len(changed))
    finally:
        if staging.exists():
            # Only this call's uniquely named staging directory is removed.
            _inside(cache, staging)
            _inventory(staging)
            shutil.rmtree(staging)


def ensure_later(home, executable, *, metrics=None, profile_id=None):
    """Run ensure() for an already running desktop on a background thread.

    Selecting a running profile or opening its task never waits for, or fails
    on, a Browser repair. One job runs per home; a request while it runs
    queues one more pass with its own arguments, so a change after the running
    pass's check is still seen. Each pass is recorded in the launch metrics,
    a failure by its exception class only. Returns the started thread, or None
    when the request was queued behind a running job, or there is nothing to
    prepare or no thread to prepare it now.
    """
    try:
        bundled = _source(executable).exists()
    except (OSError, ValueError):
        bundled = True  # The job records why it cannot check.
    if not bundled:
        return None  # Desktop editions without Browser.
    key = os.path.normcase(os.path.abspath(home))
    request = (home, executable, metrics, profile_id)
    with _jobs_lock:
        if key in _jobs:
            _jobs[key] = request
            return None
        _jobs[key] = None
    thread = threading.Thread(target=_run_later, args=(key, request),
                              name='browser-bundle-repair', daemon=True)
    try:
        thread.start()
    except RuntimeError:
        # No thread is available now; the next selection asks again.
        with _jobs_lock:
            _jobs.pop(key, None)
        return None
    return thread


def _run_later(key, request):
    try:
        while request is not None:
            home, executable, metrics, profile_id = request
            started, error = time.perf_counter(), None
            try:
                ensure(home, executable)
            except Exception as failure:
                error = type(failure).__name__  # The message can carry paths.
            if metrics is not None:
                metrics.record(profile_id, 'browser_bundle_background', started, error is None, error=error)
            with _jobs_lock:
                request = _jobs[key]
                if request is None:
                    del _jobs[key]
                else:
                    _jobs[key] = None
    except BaseException:
        with _jobs_lock:
            _jobs.pop(key, None)
        raise
