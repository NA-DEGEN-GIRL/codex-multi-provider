"""Finish an explicitly requested package update after workspace full exit.

No application is launched or terminated. A stale transaction, live parent, or
another update worker causes this one-shot helper to leave the update pending.
"""
import argparse
import json
from pathlib import Path
import time

from manager_core.instances import process_identity
from manager_core.managed_package_update import MODE, finalize
from manager_core.updates import UpdateManager, UpdateError, _lock_file, _unlock_file


def before_start(root, *, timeout=900):
    """Keep new Codex processes out until a pending registration attempt settles."""
    manager = UpdateManager(root)
    previous = manager.status()
    if previous.get('mode') != MODE or not (previous.get('status') == 'registration_pending'
            or previous.get('registration_finalize') and
            (previous.get('recovery_required') or previous.get('status') in ('preparing', 'installing', 'recovery_required'))):
        return {'status': 'not_pending'}
    deadline = time.monotonic() + timeout
    waited = False
    while True:
        try:
            claim = _lock_file(manager.directory / 'worker.lock')
            break
        except UpdateError:
            if time.monotonic() >= deadline:
                return {'status': 'startup_blocked', 'message': 'Windows 업데이트 적용이 아직 진행 중입니다. 설치가 끝난 뒤 작업 공간을 다시 열어 주세요.'}
            waited = True
            time.sleep(.2)
    try:
        current = manager.status()
        # A competing helper already made its attempt. Reconcile its result,
        # never issue another install just because registration is still busy.
        if waited or current.get('transaction_id') != previous.get('transaction_id'):
            current = manager.recover()
        elif current.get('status') == 'registration_pending':
            current = finalize(manager)
        elif current.get('registration_finalize'):
            current = manager.recover()
        if current.get('status') in ('preparing', 'installing', 'recovery_required') or (
                current.get('recovery_required') and current.get('status') != 'registration_pending'):
            return {'status': 'startup_blocked', 'message': '이전 Windows 설치 작업의 종료를 확인하지 못했습니다. 결과 확인 전에는 작업 공간을 다시 시작하지 않습니다.'}
        return {'status': current.get('status'), 'message': current.get('message', '')}
    finally:
        _unlock_file(claim)


def finish(root, transaction_id, parent_pid, parent_created, *, timeout=60):
    deadline = time.monotonic() + timeout
    while True:
        parent = process_identity(parent_pid)
        if not parent or parent.get('process_created') != parent_created:
            break
        if time.monotonic() >= deadline:
            return {'status': 'parent_still_running'}
        time.sleep(.2)
    manager = UpdateManager(root)
    try:
        claim = _lock_file(manager.directory / 'worker.lock')
    except UpdateError:
        return {'status': 'another_update_running'}
    try:
        previous = manager.status()
        if (previous.get('transaction_id') != transaction_id or previous.get('mode') != MODE
                or previous.get('status') != 'registration_pending'):
            return {'status': 'superseded'}
        return finalize(manager)
    finally:
        _unlock_file(claim)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--before-start', action='store_true')
    parser.add_argument('--transaction')
    parser.add_argument('--parent-pid', type=int)
    parser.add_argument('--parent-created', type=int)
    args = parser.parse_args()
    if args.before_start:
        print(json.dumps(before_start(args.root), ensure_ascii=False))
    else:
        if not args.transaction or not args.parent_pid or not args.parent_created:
            parser.error('parent identity and transaction are required after exit')
        finish(args.root, args.transaction, args.parent_pid, args.parent_created)
