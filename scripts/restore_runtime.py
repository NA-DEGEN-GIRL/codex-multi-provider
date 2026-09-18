"""Restore the exported runtime into a NEW worktree without touching existing sources."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def restore(source: Path, destination: Path) -> str:
    manifest = json.loads((ROOT / 'patches/runtime-source.json').read_text(encoding='utf-8'))
    patch = ROOT / 'patches' / manifest['patch']
    if hashlib.sha256(patch.read_bytes()).hexdigest() != manifest['patch_sha256']:
        raise RuntimeError('Patch SHA256 does not match the source manifest.')
    source, destination = source.resolve(), destination.resolve()
    if destination.exists():
        raise RuntimeError('Destination already exists; choose a new directory.')
    def git(repo: Path, *args: str) -> str:
        return subprocess.check_output(['git', '-c', 'core.autocrlf=false', '-C', str(repo), *args], text=True).strip()
    git(source, 'cat-file', '-e', manifest['base_commit'] + '^{commit}')
    git(source, 'worktree', 'add', '--detach', str(destination), manifest['base_commit'])
    git(destination, 'apply', '--index', '--binary', str(patch))
    result = git(destination, 'write-tree')
    if result != manifest['result_tree']:
        raise RuntimeError('Restored tree does not match the source manifest; worktree retained for inspection.')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=ROOT / 'upstream')
    parser.add_argument('--destination', type=Path, default=ROOT / 'runtime')
    args = parser.parse_args()
    print('Verified runtime tree:', restore(args.source, args.destination))
