"""Update the signed Windows package without replacing isolated managed apps.

This path never closes a profile or changes its runtime. A complete, validated
managed desktop must remain available for the current adapters; WindowsApps
processes still block installation. New package compatibility is not implied.
"""
from copy import deepcopy
import hashlib
from pathlib import Path
from uuid import uuid4

from . import desktop_bundle, desktop_publication
from .updates import (UpdateError, _atomic_json, _lock_file, _needs_recovery,
                      _unlock_file, validate_identity)


MODE = 'official_package_only'
MARKER = 'manager-desktop.json'


def _fingerprint(directory):
    return hashlib.sha256((directory / MARKER).read_bytes()).hexdigest()


def _copy(root, directory):
    directory = Path(directory).resolve(strict=True)
    if directory.parent != root:
        raise UpdateError('managed_copy_unverified', '작업을 유지할 관리용 Codex 파일을 확인하지 못했습니다.')
    marker = desktop_publication.validated(directory, MARKER)
    if not marker:
        raise UpdateError('managed_copy_unverified', '작업을 유지할 관리용 Codex 파일을 확인하지 못했습니다.')
    for name in ('ChatGPT.exe', 'chrome.dll', 'resources/app.asar'):
        if name not in marker['files'] or name not in marker['hashes']:
            raise UpdateError('managed_copy_unverified', '관리용 Codex의 필수 파일 검증 정보가 없습니다.')
    return dict(directory=str(directory), marker_sha256=_fingerprint(directory),
                version=marker['source']['version'])


def inspect(manager, installed, previous=None):
    """Read-only isolation proof, independent of local/SSH job-state RPCs."""
    root = (manager.root / 'artifacts/managed-desktop').resolve()
    try:
        fallback = desktop_publication._fallback(root, MARKER, desktop_bundle.adapter_identity())
    except (OSError, ValueError, KeyError, TypeError):
        return dict(mode=MODE, copies=[], managed_versions=[], blockers=[dict(
            code='managed_copy_unverified', message='관리용 Codex 파일 상태가 변경되었습니다. 다시 확인해 주세요.')])
    if fallback is None:
        return None
    copies = {}
    blockers = []
    unresolved = []
    try:
        candidate = _copy(root, fallback[0])
        copies[candidate['directory']] = candidate
        # Preserve every copy recorded at plan time, even if its window closed.
        for old in (previous or {}).get('copies', []):
            item = _copy(root, old['directory'])
            if item != old:
                raise UpdateError('managed_copy_changed', '업데이트 준비 후 관리용 Codex 파일이 변경되었습니다.')
            copies[item['directory']] = item
        live = manager.processes(installed)
        package_root = Path(installed['install_location']).resolve()
        for process in live:
            executable = process.get('executable')
            if not executable:
                # Even the limited query was denied: another account's process
                # (e.g. a sandbox user's codex.exe), not this user's package.
                # The installer never closes applications; a package that is
                # still in use only defers or refuses registration.
                unresolved.append(process.get('process_id'))
                continue
            path = Path(executable).resolve()
            if path.is_relative_to(package_root):
                blockers.append(dict(code='package_in_use',
                    message='Windows에 설치된 원본 Codex 창을 닫아 주세요. 작업 공간의 관리용 창은 계속 사용할 수 있습니다.'))
                continue
            if path.name.casefold() != 'chatgpt.exe' or path.parent.parent != root:
                blockers.append(dict(code='unmanaged_instance',
                    message='관리용 복사본 밖에서 실행 중인 Codex를 닫은 뒤 공식 앱을 업데이트할 수 있습니다.'))
                continue
            if str(path.parent) not in copies:
                item = _copy(root, path.parent)
                copies[item['directory']] = item
    except (OSError, ValueError, KeyError, TypeError, UpdateError) as error:
        blockers.append(dict(code=getattr(error, 'code', 'managed_copy_unverified'),
            message=str(error) if isinstance(error, UpdateError) else
                    '작업을 유지할 관리용 Codex 파일 또는 실행 위치를 확인하지 못했습니다.'))
    result = dict(mode=MODE, copies=list(copies.values()), blockers=blockers,
                  managed_versions=sorted({item['version'] for item in copies.values()}))
    if unresolved:
        result['unresolved_processes'] = len(unresolved)
    return result


def complete_message(proof):
    versions = ', '.join(proof.get('managed_versions', []))
    return ('Windows의 공식 Codex 앱 업데이트를 확인했습니다. 진행 중인 작업은 유지했습니다. '
            f'작업 공간은 검증된 관리용 Codex {versions}을 계속 사용합니다. 새 화면 적용은 별도 호환 지원이 필요합니다.')


def _registration_pending(manager, transaction, actual):
    return manager._journal(transaction, 'registration_pending',
        '공식 Codex 업데이트 파일이 준비됐습니다. «준비된 업데이트 적용»을 누르면 작업 공간에서 설치를 마칩니다. 원본 앱을 열 필요는 없습니다.',
        observed_installed=actual, recovery_required=True, install_retried=False,
        profiles_preserved=True, managed_versions=transaction['isolation']['managed_versions'])


def finalize(manager):
    """Explicit registration attempt after a completed deferred installation.

    Status checks never call this. Every attempt gets a fresh installer receipt;
    uncertain earlier workers must settle before another request is admitted.
    Windows may reject busy packages, but no running application is forced out.
    """
    try:
        claim = _lock_file(manager.directory / 'apply.lock')
    except UpdateError:
        return dict(status='recovery_required', message='다른 업데이트의 결과를 먼저 확인해야 합니다.')
    transaction, started = None, False
    try:
        previous = manager.status()
        if (previous.get('mode') != MODE or previous.get('status') != 'registration_pending'
                or previous.get('install_outcome') not in ('command_completed', 'registration_busy')):
            return previous
        current = recover(manager, previous)
        if current.get('status') != 'registration_pending':
            return current
        actual = manager.inventory()
        validate_identity(actual, previous['installed_before'])
        proof = inspect(manager, actual, previous['isolation'])
        if proof is None or proof['blockers']:
            return manager._journal(previous, 'registration_pending',
                (proof or {}).get('blockers', [{}])[0].get('message', '설치할 수 있는 상태인지 다시 확인해 주세요.'),
                blockers=(proof or {}).get('blockers', []))
        _atomic_json(manager.directory / 'history' / (previous['transaction_id'] + '.json'), previous)
        transaction = dict(transaction_id=str(uuid4()), plan_id=previous['plan_id'], mode=MODE,
            registration_finalize=True, parent_transaction_id=previous['transaction_id'],
            defer_registration=False, installed_before=actual, target=deepcopy(previous['target']),
            isolation=proof, restore_manifest=[], remote_pending=[], closed_profiles=[], close_intents=[])
        manager._journal(transaction, 'preparing', '준비된 공식 Codex 업데이트의 서명을 다시 확인합니다.')
        path, digest = manager._download(transaction['target'])
        manager._validate_download(path, digest, actual, transaction['target'])
        refreshed = manager.inventory()
        validate_identity(refreshed, actual)
        if refreshed['version'] != actual['version']:
            raise UpdateError('package_changed', '설치 버전이 변경되었습니다. 다시 확인해 주세요.')
        proof = inspect(manager, actual, proof)
        if proof is None or proof['blockers']:
            return _blocked(manager, transaction, proof)
        manager._journal(transaction, 'installing', '원본 앱을 열지 않고 Windows 업데이트 적용을 마칩니다.',
                         install_outcome='unknown')
        started = True
        manager._install_transaction = transaction
        try:
            manager._install(path, digest)
        finally:
            manager._install_transaction = None
        transaction['install_outcome'] = 'command_completed'
        return recover(manager, transaction)
    except Exception as error:
        transaction = transaction or manager.status()
        if started:
            outcome = manager.installer.inspect(transaction)
            if outcome.get('installer_settled') is True and outcome.get('installer_result_code') == 'package_in_use':
                transaction['install_outcome'] = 'registration_busy'
                return _registration_busy(manager, transaction)
        return manager._journal(transaction, 'recovery_required' if started else 'failed_before_install',
            str(error) if isinstance(error, UpdateError) else '공식 Codex 업데이트 적용 결과를 확인해야 합니다.',
            code=getattr(error, 'code', 'registration_failed'), recovery_required=started)
    finally:
        _unlock_file(claim)


def _registration_busy(manager, transaction):
    return manager._journal(transaction, 'registration_pending',
        'Windows가 사용 중인 앱 때문에 적용을 보류했습니다. 작업 공간을 완전 종료하면 자동으로 적용합니다. 원본 앱을 열 필요는 없습니다. 작업 중에는 종료하지 않아도 됩니다.',
        install_outcome='registration_busy', recovery_required=True, profiles_preserved=True)


def apply(manager, trusted, plan_path):
    try:
        claim = _lock_file(manager.directory / 'apply.lock')
    except UpdateError:
        return dict(status='recovery_required', message='다른 업데이트의 결과를 먼저 확인해야 합니다.')
    transaction = dict(transaction_id=str(uuid4()), plan_id=trusted['plan_id'], mode=MODE,
        defer_registration=True,
        installed_before=trusted['installed'], target=trusted['latest'],
        isolation=deepcopy(trusted['isolation']), restore_manifest=[], remote_pending=[],
        closed_profiles=[], close_intents=[])
    install_started = False
    try:
        if _needs_recovery(manager.status()):
            return dict(status='recovery_required', message='중단된 업데이트 결과를 먼저 확인해야 합니다.')
        installed = manager.inventory()
        validate_identity(installed)
        if installed['version'] != trusted['installed']['version']:
            return manager._journal(transaction, 'blocked', '설치 버전이 변경되었습니다. 다시 확인해 주세요.')
        proof = inspect(manager, installed, trusted['isolation'])
        if proof is None or proof['blockers']:
            return _blocked(manager, transaction, proof)
        transaction['isolation'] = proof
        manager._journal(transaction, 'preparing', '작업은 유지하며 공식 Codex 설치 파일과 서명을 확인합니다.')
        path, digest = manager._download(trusted['latest'])
        manager._validate_download(path, digest, installed, trusted['latest'])
        # Download may take minutes. Recheck package users and protected copies.
        actual = manager.inventory()
        validate_identity(actual, installed)
        if actual['version'] != installed['version']:
            return manager._journal(transaction, 'blocked', '다운로드 중 설치 버전이 변경되었습니다. 다시 확인해 주세요.')
        proof = inspect(manager, actual, proof)
        if proof is None or proof['blockers']:
            return _blocked(manager, transaction, proof)
        transaction['isolation'] = proof
        manager._journal(transaction, 'installing', '작업 공간의 실행을 유지하며 Windows 공식 Codex만 업데이트합니다.',
                         install_outcome='unknown')
        install_started = True
        manager._install_transaction = transaction
        try:
            # The signed installer defers registration rather than closing apps.
            manager._install(path, digest)
        finally:
            manager._install_transaction = None
        manager._journal(transaction, 'installing', '공식 설치 명령이 완료되어 설치 버전을 확인합니다.',
                         install_outcome='command_completed')
        actual = manager.inventory()
        validate_identity(actual, installed)
        if actual['version'] not in (trusted['latest']['version'], installed['version']):
            raise UpdateError('install_not_verified', '공식 Codex 설치 버전이 목표 버전과 일치하지 않습니다.')
        # An installed package is consumed even if a later preservation check fails.
        trusted['status'] = 'consumed'
        _atomic_json(plan_path, trusted)
        manager._check_cache = None
        _preserved(manager, proof)
        if actual['version'] == installed['version']:
            return _registration_pending(manager, transaction, actual)
        return manager._journal(transaction, 'complete', complete_message(proof),
            installed_after=actual, recovery_required=False, profiles_preserved=True,
            managed_versions=proof['managed_versions'])
    except Exception as error:
        settled = not install_started or transaction.get('install_outcome') == 'command_completed'
        if not settled:
            settled = manager.installer.inspect(transaction).get('installer_settled') is True
            if settled:
                transaction['install_outcome'] = 'settled_after_error'
        return manager._journal(transaction, 'recovery_required' if install_started else 'failed_before_install',
            str(error) if isinstance(error, UpdateError) else '공식 Codex 업데이트 결과를 확인해야 합니다.',
            code=getattr(error, 'code', 'update_failed'), recovery_required=install_started or not settled)
    finally:
        _unlock_file(claim)


def _blocked(manager, transaction, proof):
    blockers = (proof or {}).get('blockers') or [dict(code='managed_copy_unverified',
        message='현재 설정으로 다시 열 수 있는 관리용 Codex를 확인하지 못했습니다.')]
    return manager._journal(transaction, 'blocked', blockers[0]['message'], blockers=blockers)


def _preserved(manager, proof):
    root = (manager.root / 'artifacts/managed-desktop').resolve()
    for old in proof['copies']:
        if _copy(root, old['directory']) != old:
            raise UpdateError('managed_copy_changed', '공식 앱 설치 후 관리용 Codex 파일 확인이 필요합니다.')
    if desktop_publication._fallback(root, MARKER, desktop_bundle.adapter_identity()) is None:
        raise UpdateError('managed_copy_unverified', '다음 실행에 사용할 관리용 Codex 파일 확인이 필요합니다.')


def recover(manager, transaction):
    """Reconcile an interrupted package-only install; never retry installation."""
    proof = transaction['isolation']
    if transaction.get('install_outcome') == 'unknown':
        outcome = manager.installer.inspect(transaction)
        if outcome.get('installer_settled') is not True:
            return manager._journal(transaction, 'recovery_required', '기존 Windows 설치 작업의 종료를 확인하고 있습니다.',
                                    install_retried=False, recovery_required=True)
        transaction['install_outcome'] = 'settled_after_recovery'
        if outcome.get('installer_phase') == 'succeeded':
            transaction['install_outcome'] = 'command_completed'
        if outcome.get('deployment_evidence'):
            transaction['deployment_evidence'] = outcome['deployment_evidence']
        if outcome.get('installer_result_code') == 'package_in_use':
            transaction['installer_result_code'] = 'package_in_use'
            if transaction.get('registration_finalize'):
                transaction['install_outcome'] = 'registration_busy'
    actual = manager.inventory()
    validate_identity(actual, transaction['installed_before'])
    _preserved(manager, proof)
    if actual['version'] == transaction['target']['version']:
        manager._check_cache = None
        plan_path = manager.directory / 'plans' / (transaction['plan_id'] + '.json')
        import json
        plan = json.loads(plan_path.read_text(encoding='utf-8'))
        plan['status'] = 'consumed'
        _atomic_json(plan_path, plan)
        return manager._journal(transaction, 'complete', complete_message(proof),
            installed_after=actual, recovery_required=False, install_retried=False,
            profiles_preserved=True, managed_versions=proof['managed_versions'])
    if actual['version'] == transaction['installed_before']['version']:
        if transaction.get('registration_finalize') and transaction.get('install_outcome') == 'registration_busy':
            return _registration_busy(manager, transaction)
        if transaction.get('defer_registration') is True and transaction.get('install_outcome') == 'command_completed':
            return _registration_pending(manager, transaction, actual)
        return manager._journal(transaction, 'failed_install', '공식 앱 설치가 적용되지 않았습니다. 작업 공간의 실행은 유지했습니다.',
            observed_installed=actual, recovery_required=False, install_retried=False)
    return manager._journal(transaction, 'recovery_required', '다른 설치 버전이 확인되어 기존 업데이트 결과를 확인해야 합니다.',
        observed_installed=actual, recovery_required=True, install_retried=False)
