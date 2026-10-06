"""Stage built companions for explicit headless tests; never activate a release."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
from uuid import uuid4

from manager_core.store import atomic_json
from remote_helpers.package_runtime import contains_marker

ROOT = Path(__file__).resolve().parents[1]
BINARIES = ('codex', 'codex-code-mode-host', 'codex-windows-sandbox-setup',
            'codex-command-runner', 'codex-app-server')
CROSS_MANIFEST = 'windows-build.json'
CROSS_TARGET = 'x86_64-pc-windows-msvc'


def _digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _load_cross_build(root, source):
    """A server cross-build package for exactly the recorded runtime patch."""
    build = json.loads((source / CROSS_MANIFEST).read_text(encoding='utf-8'))
    recorded = json.loads((root / 'patches/runtime-source.json').read_text(encoding='utf-8'))
    if (build.get('schema') != 1 or build.get('target') != CROSS_TARGET
            or build.get('profile') != 'release' or build.get('build_kind') != 'linux-cross-xwin'
            or not isinstance(build.get('files'), dict)):
        raise RuntimeError('Unsupported cross-build package: ' + str(source))
    if (build.get('result_tree') != recorded['result_tree']
            or build.get('build_source_sha256') != recorded['patch_sha256']
            or build.get('base_commit') != recorded['base_commit']):
        raise RuntimeError('The cross-build package was built from another runtime patch.')
    files = build['files']
    for name in BINARIES:
        path = source / (name + '.exe')
        if not path.is_file() or _digest(path) != (files.get(path.name) or {}).get('sha256'):
            raise RuntimeError('Cross-built companion is missing or changed: ' + path.name)
    return build


def stage(root=ROOT, profile='release', source=None):
    # The managed runtime parses and hashes very large shared histories (hundreds
    # of MB for a long task). An unoptimized debug build made a Claude handoff
    # spend ~90 s reading one; ship the optimized release profile.
    if profile not in ('release', 'debug'):
        raise ValueError('profile must be release or debug')
    root = Path(root).resolve()
    cross = None
    if source is None:
        source = root / 'work/target-runtime' / profile
    else:
        # A package cross-built on the Linux server (package_windows_runtime.py).
        if profile != 'release':
            raise ValueError('cross-built packages are release builds')
        source = Path(source).resolve()
        cross = _load_cross_build(root, source)
    for name in BINARIES:
        if not (source / (name + '.exe')).is_file():
            raise RuntimeError('Missing built companion: ' + name)
    release_id = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S') + '-' + uuid4().hex[:6]
    destination = root / 'artifacts/manager-runtime/releases' / release_id
    destination.mkdir(parents=True)
    try:
        return _stage_into(root, destination, source, profile, cross)
    except BaseException:
        # Never leave a release folder without candidate.json behind.
        shutil.rmtree(destination, ignore_errors=True)
        raise


def _stage_into(root, destination, source, profile, cross):
    files = {}
    for name in BINARIES:
        target = destination / (name + '.exe')
        shutil.copyfile(source / target.name, target)
        files[target.name] = _digest(target)
        if cross is not None and files[target.name] != cross['files'][target.name]['sha256']:
            raise RuntimeError('Cross-built companion changed while staging: ' + target.name)
    binary = destination / 'codex.exe'
    version = subprocess.check_output([str(binary), '--version'], text=True, timeout=15).strip()
    if cross is None:
        base = subprocess.check_output(['git', '-C', str(root / 'runtime'), 'rev-parse', 'HEAD'], text=True).strip()
    else:
        base = cross['base_commit']
    manifest = dict(runtime=str(binary), version=version, source_base=base, build_profile=profile,
                    sha256=files[binary.name], files=files, validation='candidate',
                    staged_at=datetime.now(timezone.utc).isoformat(),
                    capabilities={name: True for name in (
                        'managed_store_binding', 'managed_close_idle', 'managed_idle_status',
                        'managed_reload_binding', 'native_record_catalog', 'paginated_record_catalog',
                        'shared_record_catalog', 'native_catalog_updates', 'native_catalog_membership',
                        'native_catalog_retry', 'native_catalog_names')})
    manifest['capabilities']['mixed_source_catalog'] = contains_marker(
        binary, b'invalid mixed source catalog legacy identity')
    manifest['capabilities']['shared_record_execution'] = contains_marker(
        binary, b'The same task ID exists in multiple record homes.')
    manifest['capabilities']['shared_new_task_storage'] = contains_marker(
        binary, b'New task storage is not an enrolled common record home.')
    manifest['capabilities']['shared_append_envelopes'] = contains_marker(
        binary, b'CODEX_RECORD_SHARED_APPEND')
    manifest['capabilities']['shared_history_refresh'] = contains_marker(
        binary, b'shared task was replaced during history refresh')
    manifest['capabilities']['canonical_record_storage'] = contains_marker(
        binary, b'CODEX_RECORD_HOME must be an absolute storage root')
    manifest['capabilities']['claude_code_agent'] = contains_marker(
        binary, b'CODEX_CLAUDE_CODE_AGENT_V1')
    manifest['capabilities']['managed_execution_presets'] = contains_marker(
        binary, b'CODEX_MANAGER_EXECUTION_PRESETS')
    if cross is not None:
        manifest.update(build_origin=cross['build_kind'], source_tree=cross['result_tree'],
                        build_source_sha256=cross['build_source_sha256'],
                        build_toolchain=cross.get('toolchain'), build_xwin=cross.get('xwin'))
    path = destination / 'candidate.json'
    atomic_json(path, manifest)
    print(json.dumps(dict(candidate_manifest=str(path), active_runtime_changed=False)))
    return path


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', choices=('release', 'debug'), default='release')
    parser.add_argument('--source', type=Path,
                        help='Package directory from package_windows_runtime.py (Linux cross build).')
    arguments = parser.parse_args()
    stage(profile=arguments.profile, source=arguments.source)
