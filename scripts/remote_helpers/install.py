"""Run over SSH stdin; extract only verified manager-owned files on Linux.

This module is also imported by local tests with an explicit temporary base.
It does not start Codex, replace the installed CLI, or activate a configuration.
"""
from __future__ import annotations

import hashlib
import errno
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
import tarfile
import tempfile
import uuid


DISPATCHER = '''from pathlib import Path
import json,os,re,runpy,sys
sys.dont_write_bytecode=True
profile=Path(__file__).resolve().parent
revision=sys.argv[1] if len(sys.argv)>1 else ""
if not re.fullmatch(r"[0-9a-f]{64}",revision): raise SystemExit("Invalid manager revision")
descriptor=profile/"definitions"/(revision+".json")
if descriptor.is_symlink(): raise SystemExit("Invalid manager descriptor")
data=json.loads(descriptor.read_text())
helpers=Path(data["helpers"])
if helpers.is_symlink() or helpers.parent.resolve()!=(profile/"helpers").resolve(): raise SystemExit("Invalid manager helpers")
os.environ["CODEX_MANAGER_PROFILE_DIR"]=str(profile)
sys.path.insert(0,str(helpers))
runpy.run_path(str(helpers/"launch.py"),run_name="__main__")
'''


def _safe(path):
    relative = PurePosixPath(path)
    if (not isinstance(path, str) or "\\" in path or ":" in path or
            relative.is_absolute() or not relative.parts or ".." in relative.parts):
        raise ValueError("invalid member")
    return relative


def _owned_directory(path):
    """Refuse symlink ancestors; the managed root must belong to this user."""
    for parent in reversed((path, *path.parents)):
        if parent.is_symlink():
            raise ValueError("symlink directory")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if hasattr(os, "getuid") and path.stat().st_uid != os.getuid():
        raise ValueError("foreign directory")
    os.chmod(path, 0o700)


def _same_files(source, target):
    def inventory(root):
        files = set()
        for path in root.rglob("*"):
            if path.is_symlink() or not (path.is_file() or path.is_dir()):
                raise ValueError("nonregular cached member")
            if path.is_file():
                files.add(path.relative_to(root).as_posix())
        return files
    if inventory(source) != inventory(target):
        return False
    for file in source.rglob("*"):
        if file.is_file():
            existing = target / file.relative_to(source)
            if existing.is_symlink() or not existing.is_file():
                return False
            with file.open("rb") as a, existing.open("rb") as b:
                if hashlib.file_digest(a, "sha256").digest() != hashlib.file_digest(b, "sha256").digest():
                    return False
    return True


def _cached_runtime(base, bundle, files):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", bundle):
        raise ValueError('invalid bundle')
    runtime = base / 'runtime' / bundle
    if any(p.is_symlink() for p in (runtime, *runtime.parents)) or not runtime.is_dir():
        return False
    if hasattr(os, 'getuid') and runtime.stat().st_uid != os.getuid():
        return False
    expected = {str(_safe(item['path'])): item for item in files}
    if not {'codex', 'codex-code-mode-host', 'bwrap'} <= expected.keys():
        return False
    actual = set()
    for path in runtime.rglob('*'):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):return False
        if not path.is_file():continue
        name = path.relative_to(runtime).as_posix()
        item = expected.get(name)
        if not item or path.stat().st_size != item.get('size'):return False
        with path.open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != item['sha256']:return False
        actual.add(name)
    return actual == expected.keys()


def preflight(request, base=None):
    """Read-only cache/disk check. Paths and hashes are supplied by the manager."""
    base = Path(base) if base is not None else Path.home() / '.local/share/codex-control-center'
    if any(p.is_symlink() for p in (base, *base.parents)):raise ValueError('symlink directory')
    candidates = request['candidates']
    if not isinstance(candidates, list) or not 1 <= len(candidates) <= 16:raise ValueError('candidate count')
    for candidate in candidates:
        if _cached_runtime(base, candidate['bundle_id'], candidate['files']):
            return {'status':'cached', 'runtime_bundle':candidate['bundle_id']}
    parent = base
    while not parent.exists():parent = parent.parent
    required = sum(item['size'] for item in candidates[0]['files']) + 32*1024*1024
    free = shutil.disk_usage(parent).free
    return {'status':'upload' if free >= required else 'insufficient_space',
            'required_bytes':required, 'free_bytes':free}


def failure(error):
    code = ('remote_disk_full' if isinstance(error, OSError) and error.errno in (errno.ENOSPC, errno.EDQUOT)
            else 'remote_install_failed')
    return {'status':'failed','code':code}


def install(stream, base=None):
    verify_host = base is None
    base = Path(base) if base is not None else Path.home() / ".local/share/codex-control-center"
    _owned_directory(base)
    stage = Path(tempfile.mkdtemp(prefix=".incoming-", dir=base))
    try:
        with tarfile.open(fileobj=stream, mode="r|*") as archive:
            first = next(iter(archive), None)
            if first is None or first.name != "manifest.json" or not first.isfile() or first.size > 262144:
                raise ValueError("manifest missing")
            manifest = json.load(archive.extractfile(first))
            profile_id, revision, bundle = manifest["profile_id"], manifest["revision"], manifest["bundle_id"]
            if (manifest.get("schema") != 1 or str(uuid.UUID(profile_id)) != profile_id or
                    not re.fullmatch(r"[0-9a-f]{64}", revision) or
                    not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", bundle)):
                raise ValueError("manifest invalid")
            if verify_host:
                identity = Path("/etc/machine-id").read_text().strip()
                actual = hashlib.sha256((identity + "\\0" + str(os.getuid()) + "\\0" + str(Path.home())).encode()).hexdigest()
                if manifest.get("host_identity") != actual:
                    raise ValueError("SSH host or OS user changed")
            expected = {}
            for item in manifest["files"]:
                name = str(_safe(item["path"]))
                if name in expected or name.split("/", 1)[0] not in {"runtime", "definition", "helpers"}:
                    raise ValueError("manifest path invalid")
                if not re.fullmatch(r"[0-9a-f]{64}", item["sha256"]) or item["mode"] not in (0o600, 0o700):
                    raise ValueError("manifest item invalid")
                expected[name] = item
            if not 4 <= len(expected) <= 256:
                raise ValueError("manifest count invalid")
            if not {"runtime/codex", "runtime/codex-code-mode-host", "runtime/bwrap", "definition/config.toml", "helpers/launch.py", "helpers/common.py", "helpers/native_controller.py"} <= expected.keys():
                raise ValueError("required member missing")
            referenced = {name for name in expected if name.startswith('runtime/')} if manifest.get('reuse_runtime') is True else set()
            if referenced and not _cached_runtime(base,bundle,[{**expected[name],'path':name[8:]} for name in referenced]):
                raise ValueError('cached runtime changed')
            seen = set(referenced)
            total = 0
            # archive's iterator can revisit its cached first member; advance explicitly.
            while True:
                member = archive.next()
                if member is None:
                    break
                name = str(_safe(member.name))
                total += member.size
                if (not member.isfile() or name not in expected or name in seen or
                        member.size < 0 or total > 8 * 1024**3):
                    raise ValueError("unexpected member")
                seen.add(name)
                destination = stage.joinpath(*PurePosixPath(name).parts)
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                digest = hashlib.sha256()
                with archive.extractfile(member) as incoming, destination.open("xb") as output:
                    while data := incoming.read(1024 * 1024):
                        digest.update(data)
                        output.write(data)
                if digest.hexdigest() != expected[name]["sha256"]:
                    raise ValueError("hash mismatch")
                os.chmod(destination, expected[name]["mode"])
            if seen != set(expected):
                raise ValueError("archive incomplete")
        # Hash validation finishes before any active destination is touched.
        runtime_parent = base / "runtime"
        profile = base / "profiles" / profile_id
        definition_parent = profile / "definitions"
        for folder in (runtime_parent, profile, definition_parent):
            _owned_directory(folder)
        runtime = runtime_parent / bundle
        definition = definition_parent / revision
        destinations = [(stage / 'definition', definition)]
        if not referenced:destinations.insert(0,(stage / 'runtime',runtime))
        for source, destination in destinations:
            if destination.exists() or destination.is_symlink():
                if destination.is_symlink() or not destination.is_dir() or not _same_files(source, destination):
                    raise ValueError("immutable destination conflict")
            else:
                os.replace(source, destination)
        # Cache the entire helper version so old revisions keep their launcher and
        # controller implementation when a new bundle is prepared.
        helper_source = stage / "helpers"
        helper_hashes = {name: item["sha256"] for name, item in expected.items() if name.startswith("helpers/")}
        # Loader changes receive a fresh immutable helper directory. Older
        # revisions may contain Python-generated __pycache__ files; they stay
        # untouched and are never silently excluded from integrity checks.
        helper_hashes['dispatcher'] = hashlib.sha256(DISPATCHER.encode()).hexdigest()
        helper_digest = hashlib.sha256(json.dumps(helper_hashes, sort_keys=True).encode()).hexdigest()
        helper_directory = profile / "helpers" / helper_digest
        _owned_directory(helper_directory.parent)
        if helper_directory.exists() or helper_directory.is_symlink():
            if helper_directory.is_symlink() or not helper_directory.is_dir() or not _same_files(helper_source, helper_directory):
                raise ValueError("cached helper mismatch")
        else:
            os.replace(helper_source, helper_directory)
        # The immutable profile definition is selected by a separate launch request.
        descriptor = {"schema": 1, "profile_id": profile_id, "revision": revision,
                      "runtime": str(runtime), "definition": str(definition), "helpers": str(helper_directory),
                      "host_identity": manifest.get("host_identity")}
        if manifest.get('managed_sources') is True:
            if 'helpers/managed_sources.py' not in expected:
                raise ValueError('managed source helper missing')
            descriptor['managed_sources'] = True
        if manifest.get('source_catalog') is True:
            if not descriptor.get('managed_sources'):
                raise ValueError('source catalog requires managed sources')
            descriptor['source_catalog'] = True
        if manifest.get('mixed_source_catalog') is True:
            if not descriptor.get('source_catalog') or 'helpers/catalog_legacy.py' not in expected:
                raise ValueError('mixed source catalog requires discovery helper and source catalog')
            descriptor['mixed_source_catalog'] = True
        descriptor_path = profile / "definitions" / (revision + ".json")
        if descriptor_path.is_symlink():
            raise ValueError("symlink descriptor")
        if descriptor_path.exists() and json.loads(descriptor_path.read_text(encoding="utf-8")) != descriptor:
            raise ValueError("immutable descriptor conflict")
        temporary = profile / (".descriptor-" + uuid.uuid4().hex)
        temporary.write_text(json.dumps(descriptor), encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, descriptor_path)
        entry = profile / "launch.py"
        if entry.is_symlink():
            raise ValueError("symlink launcher")
        # The stable dispatcher selects immutable code by the supplied revision.
        temporary = profile / (".launcher-" + uuid.uuid4().hex)
        temporary.write_text(DISPATCHER, encoding="utf-8")
        os.chmod(temporary, 0o700)
        os.replace(temporary, entry)
        return {"status": "prepared", "revision": revision, "runtime_bundle": bundle}
    finally:
        # stage comes only from mkdtemp immediately inside the checked managed root.
        if stage.parent.resolve() == base.resolve() and stage.name.startswith(".incoming-"):
            shutil.rmtree(stage, ignore_errors=True)


if __name__ == "__main__":
    try:
        print(json.dumps(preflight(json.load(sys.stdin)) if '--preflight' in sys.argv else install(sys.stdin.buffer)))
    except Exception as error:
        # Remote stderr may be presented by SSH; never print payloads or exception text.
        print(json.dumps(failure(error)))
        sys.exit(1)
