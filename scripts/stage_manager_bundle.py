"""Freeze Python + native bridges before publishing a manager release pointer."""
import hashlib
import json
from pathlib import Path
import shutil
import sys


def stage(root, release):
    root, release = Path(root).resolve(), Path(release).resolve()
    target = release / 'scripts'
    if target.exists() or (release / 'runtime-manifest.json').exists():
        raise RuntimeError('A frozen release must never be overwritten.')
    for source in sorted([*(root / 'scripts').rglob('*.py'), *(root / 'scripts/manager_core').glob('*.cjs')]):
        if '__pycache__' in source.parts or source.is_symlink():
            continue
        destination = target / source.relative_to(root / 'scripts')
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    for relative in ('scripts/control_center.py', 'scripts/manager_core/runtime_proxy.py',
                     'scripts/manager_core/ssh_shim.py', 'Codex.ControlCenter.RuntimeProxy.dll', 'ssh/ssh.dll', 'codex-workspace-service.exe'):
        if not (release / relative).is_file():
            raise RuntimeError('Incomplete manager runtime bundle: ' + relative)
    files = [*target.rglob('*.py'), *target.rglob('*.cjs')]
    for prefix in ('Codex.ControlCenter.RuntimeProxy', 'ssh/ssh'):
        files.extend(path for path in release.glob(prefix + '.*') if path.is_file())
    hashes = {path.relative_to(release).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
              for path in sorted(files)}
    revision = hashlib.sha256(json.dumps(hashes, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    service_hashes = dict(hashes)
    for name in ('codex-workspace-service.exe', 'Codex.ControlCenter.Shared.dll'):
        if (release / name).exists():
            service_hashes[name] = hashlib.sha256((release / name).read_bytes()).hexdigest()
    service_revision = hashlib.sha256(json.dumps(service_hashes, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    (release / 'python-path.txt').write_text(sys.executable, encoding='utf-8')
    value = dict(version=1, runtime_revision=revision, service_revision=service_revision, files=hashes)
    (release / 'runtime-manifest.json').write_text(json.dumps(value, indent=2) + '\n', encoding='utf-8')
    return value


if __name__ == '__main__':
    print(stage(*sys.argv[1:])['runtime_revision'])
