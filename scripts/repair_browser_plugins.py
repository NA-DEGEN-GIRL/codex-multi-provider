"""Repair bundled Browser code for verified running profiles without restarting.

Reads process identity and adds/repairs package files only. Does not inspect
credentials, browser sessions, settings or history, and never stops a process.
"""
import argparse
import json
import os
from pathlib import Path

from manager_core.browser_bundle import ensure
from manager_core.instances import process_identity
from manager_core.store import Store, identifier


def repair(root, profile_id=None):
    root = Path(root).resolve()
    store = Store(root)
    profiles = [store.profile(identifier(profile_id))] if profile_id else store.read()['profiles']
    results = []
    for profile in profiles:
        if profile.get('removed_at'):
            continue
        result = dict(profile=profile.get('alias') or profile['id'])
        active = process_identity(profile.get('process_id'))
        if (not active or active.get('process_created') != profile.get('process_created')
                or os.path.normcase(active['executable_path']) != os.path.normcase(profile.get('executable_path', ''))):
            results.append({**result, 'state': 'not_running'})
            continue
        home = Path(profile['home'])
        expected = store.directory / 'profiles' / identifier(profile['id']) / 'codex'
        executable = Path(active['executable_path'])
        if home.resolve() != expected.resolve() or not executable.resolve().is_relative_to(root / 'artifacts/managed-desktop'):
            raise ValueError('관리 프로필의 브라우저 복구 경로를 확인하지 못했습니다.')
        results.append({**result, **ensure(home, executable)})
    return dict(profiles=results, processes_restarted=0)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument('--profile')
    selection.add_argument('--all-running', action='store_true')
    args = parser.parse_args()
    print(json.dumps(repair(args.root, args.profile), ensure_ascii=False))
