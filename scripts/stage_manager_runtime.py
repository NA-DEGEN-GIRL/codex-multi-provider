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


def stage(root=ROOT, profile='release'):
    # The managed runtime parses and hashes very large shared histories (hundreds
    # of MB for a long task). An unoptimized debug build made a Claude handoff
    # spend ~90 s reading one; ship the optimized release profile.
    if profile not in ('release', 'debug'):
        raise ValueError('profile must be release or debug')
    root = Path(root).resolve()
    source = root / 'work/target-runtime' / profile
    for name in BINARIES:
        if not (source / (name + '.exe')).is_file():
            raise RuntimeError('Missing built companion: ' + name)
    release_id = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S') + '-' + uuid4().hex[:6]
    destination = root / 'artifacts/manager-runtime/releases' / release_id
    destination.mkdir(parents=True)
    files = {}
    for name in BINARIES:
        target = destination / (name + '.exe')
        shutil.copyfile(source / target.name, target)
        with target.open('rb') as stream:
            files[target.name] = hashlib.file_digest(stream, 'sha256').hexdigest()
    binary = destination / 'codex.exe'
    version = subprocess.check_output([str(binary), '--version'], text=True, timeout=15).strip()
    base = subprocess.check_output(['git', '-C', str(root / 'runtime'), 'rev-parse', 'HEAD'], text=True).strip()
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
    path = destination / 'candidate.json'
    atomic_json(path, manifest)
    print(json.dumps(dict(candidate_manifest=str(path), active_runtime_changed=False)))
    return path


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', choices=('release', 'debug'), default='release')
    stage(profile=parser.parse_args().profile)
