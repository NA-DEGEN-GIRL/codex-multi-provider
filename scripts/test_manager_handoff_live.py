"""Explicit headless, two-account canonical handoff integration test.

Default invocation only prints the test plan. --execute requires a published,
hashed, versioned runtime with every required capability. All conversations and
profile homes are newly created and unlisted in the real manager Store. Existing
02/04 logins supply access tokens in memory; their records/configs are not changed.
No GUI automation, public listener, credential copy, or uncertain-close retry.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

from live_test import collect_routes
from manager_core import authority, record_catalog
from manager_core.accounts import Accounts
from manager_core.catalog import list_catalog
from manager_core.handoff import HandoffManager
from manager_core.managed_sources import mark, manifest
from manager_core.providers import ProviderRegistry
from manager_core.proxy_auth import account_fingerprint, read_existing_tokens
from manager_core.runtime_admin import AdminClient
from manager_core.runtime_build import load_release, resolve as resolve_runtime
from manager_core.store import Store, atomic_json
from progress import Progress
from test_manager_live import ManagedClient, collect_execution_evidence, verify_output_bytes

ROOT = Path(__file__).resolve().parents[1]
REQUIRED_CAPABILITIES = ('managed_store_binding', 'managed_close_idle', 'managed_idle_status',
                         'managed_reload_binding', 'native_record_catalog')
ALLOWED_ALIASES = ('02', '04')


def checked_release(root, candidate_manifest=None):
    """No compatibility fallback to artifacts/runtime/codex.exe is allowed."""
    root = Path(root).resolve()
    pointer = Path(candidate_manifest).resolve(strict=True) if candidate_manifest else root / 'artifacts/manager-runtime/current.json'
    if candidate_manifest and not pointer.is_relative_to(root / 'artifacts/manager-runtime/releases'):
        raise RuntimeError('A candidate manifest must belong to an immutable manager runtime release.')
    if not pointer.is_file():
        raise RuntimeError('A tested versioned manager runtime has not been published.')
    runtime = load_release(root, pointer) if candidate_manifest else resolve_runtime(root)
    missing = [name for name in REQUIRED_CAPABILITIES if runtime.get('capabilities', {}).get(name) is not True]
    if missing:
        raise RuntimeError('The published runtime lacks verified capabilities: ' + ', '.join(missing))
    executable = Path(runtime['runtime']).resolve(strict=True)
    if not executable.is_relative_to(root / 'artifacts/manager-runtime/releases'):
        raise RuntimeError('Only an immutable published manager runtime is accepted.')
    release = json.loads((root / 'artifacts/manager/current.json').read_text(encoding='utf-8-sig'))
    bootstrap = Path(release['runtime_proxy']).resolve(strict=True)
    if (not bootstrap.is_relative_to(root / 'artifacts/manager/releases')
            or bootstrap.parent != Path(release['directory']).resolve(strict=True)):
        raise RuntimeError('The native bootstrap is outside its published manager directory.')
    with bootstrap.open('rb') as stream:
        bootstrap_hash = hashlib.file_digest(stream, 'sha256').hexdigest()
    return runtime, bootstrap, bootstrap_hash


def select_accounts(accounts):
    selected = []
    for alias in ALLOWED_ALIASES:
        matches = [account for account in accounts if account.get('alias') == alias]
        if len(matches) != 1:
            raise RuntimeError('Exactly one registered Codex account is required for each alias 02 and 04.')
        selected.append(matches[0])
    if selected[0]['id'] == selected[1]['id']:
        raise RuntimeError('The two aliases must refer to distinct registered accounts.')
    return selected


def isolated_store(root, run_id):
    store = Store(root)
    # Keep UUID homes inside the runtime's manager-owned profiles directory.
    # Only this run's registry is separate; the real manager never lists them.
    store.path = Path(root).resolve() / 'work/control-center/handoff-live' / run_id / 'state.json'
    return store


def record_snapshot(home):
    """Hash only this fresh source's records; never copy bodies into the report."""
    home = Path(home).resolve(strict=True)
    result = {}
    for name in ('sessions', 'archived_sessions'):
        directory = home / name
        if not directory.exists():
            continue
        if directory.is_symlink() or directory.resolve() != directory:
            raise RuntimeError('The fresh test session directory unexpectedly became a link.')
        for file in sorted(directory.rglob('*')):
            if not file.is_file():
                continue
            if file.is_symlink() or not file.resolve().is_relative_to(directory):
                raise RuntimeError('A source record escaped its isolated test HOME.')
            if file.stat().st_size > 128 * 1024 * 1024:
                raise RuntimeError('An isolated source record exceeded the test inspection limit.')
            with file.open('rb') as stream:
                result[file.relative_to(home).as_posix()] = hashlib.file_digest(stream, 'sha256').hexdigest()
    # The native read-only viewer also consults these source databases. Include
    # the main files and WAL contents; shared-memory lock bytes are not records.
    for prefix in ('state_', 'thread_history_', 'goals_', 'queue_', 'memories_'):
        for file in sorted(home.glob(prefix + '*.sqlite*')):
            if file.name.endswith('-shm'):
                continue
            if file.is_symlink() or file.resolve().parent != home or not file.is_file():
                raise RuntimeError('A source database escaped its isolated test HOME.')
            with file.open('rb') as stream:
                result[file.name] = hashlib.file_digest(stream, 'sha256').hexdigest()
    if not result:
        raise RuntimeError('The fresh canonical source has no persisted record files.')
    return result


def prepare_profile(store, account, fingerprint, registry, model_id, *, viewer=False):
    # Deliberately omit source_home from add_profile: it would register the real
    # account's existing conversation store. This fixture catalogs only new homes.
    profile = store.add_profile('test-' + account['alias'] + ('-viewer' if viewer else ''),
                                None if viewer else account['id'])
    home = Path(profile['home'])
    home.mkdir(parents=True)
    config = ('model = "gpt-6-astra"\nmodel_reasoning_effort = "low"\n'
              'web_search = "disabled"\napproval_policy = "never"\n'
              'sandbox_mode = "workspace-write"\ncli_auth_credentials_store = "file"\n'
              'tool_output_token_limit = 3000\nsuppress_unstable_features_warning = true\n'
              '[windows]\nsandbox = "unelevated"\n'
              '[features]\nenable_request_compression = false\n')
    (home / 'config.toml').write_text(config, encoding='utf-8')
    generated = registry.generate(home, not viewer, [] if viewer else [model_id])
    def update(data):
        current = store.profile(profile['id'], data)
        current.update(source_home=account['home'], account_fingerprint=fingerprint,
                       generation=str(uuid4()), view_only=viewer)
        current['policy'].update(enabled=not viewer, model_ids=[] if viewer else [model_id])
        return current
    profile = store.mutate(update)
    if not viewer:
        mark(profile)
        binding = generated['bindings'][0]
        if binding['reasoning_effort'] != 'max' or binding['wire_model_id'] != 'deepseek-flash':
            raise RuntimeError('The registered DeepSeek Flash binding must remain max.')
    else:
        binding = None
    return profile, binding


def environment_for(root, store, profile, runtime, bootstrap, provider_env, *, catalog=None, shared_catalog=None):
    env = {key: value for key, value in os.environ.items()
           if not key.upper().startswith('CODEX_') and key != 'ELECTRON_RUN_AS_NODE'}
    env.update(CODEX_HOME=profile['home'], CODEX_CLI_PATH=str(bootstrap), CODEX_MANAGER_ROOT=str(root),
               CODEX_MANAGER_PYTHON=sys.executable, CODEX_MANAGER_REAL_RUNTIME=runtime['runtime'],
               CODEX_MANAGER_PROFILE_ID=profile['id'], CODEX_MANAGER_GENERATION=profile['generation'],
               CODEX_MANAGER_OBSERVER_PATH=str(Path(root) / 'work/control-center/instances' / profile['id'] / 'runtime-state.json'),
               CODEX_MANAGER_AUTH_SOURCE=profile['source_home'],
               CODEX_MANAGER_EXPECTED_ACCOUNT_FINGERPRINT=profile['account_fingerprint'])
    if catalog is not None:
        env['CODEX_MANAGER_RECORD_CATALOG'] = str(catalog)
    else:
        env['CODEX_MANAGER_MANAGED_SOURCES'] = str(manifest(store, profile))
        env.update(provider_env)
        if shared_catalog is not None:
            env['CODEX_MANAGER_SHARED_CATALOG'] = str(shared_catalog)
    return env


class AuditedAdmin:
    """Allowlisted request metadata only; never record auth, prompts or bodies."""
    def __init__(self, root, profile, audit, before_close):
        self.profile = profile
        self.client = AdminClient(root, profile['id'], profile['generation'])
        self.audit, self.before_close = audit, before_close

    def identities(self):
        return self.client.identities()

    def request(self, method, params, timeout=15, **kwargs):
        if method == 'thread/managedCloseIdle':
            self.before_close(self.profile['id'], params['threadId'])
        result = self.client.request(method, params, timeout, **kwargs)
        self.audit.append({'profile_id': self.profile['id'], 'method': method,
                           'thread_id': params.get('threadId'), 'completed_at': time.monotonic()})
        if method == 'thread/managedIdleStatus':
            self.audit[-1]['idle_evidence'] = {key: result.get(key) for key in (
                'idle', 'blockers', 'observedThreadIds', 'proofScope', 'sourceStoreId')}
        return result


def loaded_ids(admin):
    ids, cursor = set(), None
    for _ in range(32):
        page = admin.request('thread/loaded/list', {'limit': 256, 'cursor': cursor})
        if ids.intersection(page['data']):
            raise RuntimeError('The loaded thread inventory changed while paging.')
        ids.update(page['data'])
        cursor = page['nextCursor']
        if cursor is None:
            return ids
    raise RuntimeError('The isolated runtime has an unexpectedly large loaded inventory.')


def check_execution(home, parent_id, child_id, binding, minimum_turns):
    evidence = collect_execution_evidence(home, parent_id)
    parents = [record for record in evidence['threads'] if record['thread_id'] == parent_id]
    children = [record for record in evidence['threads'] if record['parent_thread_id'] == parent_id]
    if evidence['parse_errors'] or len(parents) != 1 or len(children) != 1 or children[0]['thread_id'] != child_id:
        raise RuntimeError('The canonical parent and same external child execution records are incomplete.')
    child, parent = children[0], parents[0]
    if child['provider'] != binding['runtime_provider_id'] or len(child['turns']) < minimum_turns:
        raise RuntimeError('The external provider identity or same-child turn history is incomplete.')
    if any(turn.get('model') != 'deepseek-flash' or turn.get('effort') != 'max'
           or not turn.get('completed') or turn['successful_commands'] < 1 for turn in child['turns']):
        raise RuntimeError('Every DeepSeek Flash turn must use max and complete a shell command with exit code 0.')
    if parent['provider'] != 'openai' or any(turn.get('model') != 'gpt-6-astra' for turn in parent['turns']):
        raise RuntimeError('The parent provider or model changed during handoff.')
    if any(parent[key] for key in ('other_tool_calls', 'command_items', 'file_change_items', 'unknown_tool_items')):
        raise RuntimeError('The parent performed direct file work instead of delegating the test.')
    return evidence


def write_report(output, report, progress):
    progress.write_report(report)
    lines = ['# 계정 02 → 04 → 02: 같은 대화·자식 인계 검증', '',
             '**결과: ' + report['status'] + '**', '',
             '새 테스트 작업과 별도 프로필에서만 실행합니다. 기존 Codex 창과 실제 계정의 저장된 작업은 조작하지 않습니다.', '',
             '| 확인 항목 | 결과 |', '|---|---|']
    for name, passed in report['checks'].items():
        lines.append('| ' + name + ' | ' + ('통과' if passed else '실패') + ' |')
    if report.get('error'):
        lines.extend(['', report['error']])
    lines.extend(['', '실제 GUI의 삽입·탐색, SSH, 공식 앱 업데이트는 이 시험에 포함하지 않습니다. '
                  '인계는 쓰기 해제 증거와 원본 소유권 변경을 검증하며, 읽기 전용 idle 관측을 해제 증거로 사용하지 않습니다.', ''])
    (Path(output) / 'report.md').write_text('\n'.join(lines), encoding='utf-8')


def run(root=ROOT, candidate_manifest=None):
    root = Path(root).resolve()
    # Validate publication before reading any account credentials or making homes.
    runtime, bootstrap, bootstrap_hash = checked_release(root, candidate_manifest)
    run_id = 'manager-handoff-live-' + time.strftime('%Y%m%d-%H%M%S') + '-' + uuid4().hex[:6]
    fixture = root / 'work/control-center/handoff-live' / run_id
    workspace = fixture / 'workspace'
    output = root / 'artifacts/results' / run_id
    store = isolated_store(root, run_id)
    progress = Progress(output)
    report = {'run_id': run_id, 'status': 'RUNNING', 'checks': {}, 'accounts': [],
              'started_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'workspace': str(workspace),
              'fixture_store': str(store.path), 'runtime_sha256': runtime['sha256'],
              'runtime_version': runtime.get('version'), 'bootstrap_sha256': bootstrap_hash,
              'candidate_manifest': str(candidate_manifest) if candidate_manifest else None,
              'transfers': [], 'audit': []}
    clients, admins, profiles, identities = {}, {}, [], {}
    provider_env = {}
    close_expectation = {}
    parent_id = child_id = peer_id = None
    source_home = None
    try:
        available = Accounts(root).list()
        windows_profiles = [p for p in Store(root).read()['profiles'] if p.get('auth_mode')=='native' and not p.get('view_only') and not p.get('removed_at')]
        for alias in ALLOWED_ALIASES:
            native = [p for p in windows_profiles if p['alias']==alias]
            if native:
                if len(native)!=1 or native[0].get('login_state')!='signed_in':
                    raise RuntimeError('The Windows test account must have one verified direct login.')
                profile=native[0]
                available=[a for a in available if a['alias']!=alias]
                available.append(dict(id=profile['id'],alias=alias,home=profile['home'],
                                      expected_fingerprint=profile['account_fingerprint'],auth_source='windows_native'))
        accounts = select_accounts(available)
        fingerprints = []
        for account in accounts:
            tokens = read_existing_tokens(account['home'])
            progress.add_secret(tokens.access_token)
            progress.add_secret(tokens.account_id)
            fingerprint = account_fingerprint(tokens.account_id)
            if account.get('expected_fingerprint') and account['expected_fingerprint']!=fingerprint:
                raise RuntimeError('The Windows test account changed after direct login verification.')
            fingerprints.append(fingerprint)
            report['accounts'].append({'alias': account['alias'], 'fingerprint': fingerprint,'auth_source':account.get('auth_source','legacy_import')})
            del tokens
        if len(set(fingerprints)) != 2:
            raise RuntimeError('Aliases 02 and 04 resolved to the same authenticated account.')
        report['checks']['서로 다른 실제 계정 02·04 확인'] = True
        registry = ProviderRegistry(root)
        matches = [model for model in registry.list()['models'] if model['wire_model_id'] == 'deepseek-flash']
        if len(matches) != 1:
            raise RuntimeError('Exactly one verified DeepSeek Flash model binding is required.')
        model = matches[0]
        provider_env = registry.environment([model['id']])
        for value in provider_env.values():
            progress.add_secret(value)
        workspace.mkdir(parents=True)
        subprocess.run(['git', 'init', '-q', str(workspace)], check=True, capture_output=True,
                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        bindings = []
        for account, fingerprint in zip(accounts, fingerprints):
            profile, binding = prepare_profile(store, account, fingerprint, registry, model['id'])
            profiles.append(profile)
            bindings.append(binding)
        if bindings[0] != bindings[1]:
            raise RuntimeError('The two isolated execution profiles generated different provider bindings.')
        binding = bindings[0]
        report['binding'] = {key: binding[key] for key in ('model_id', 'role_id', 'wire_model_id', 'runtime_provider_id', 'reasoning_effort')}
        source_home = Path(profiles[0]['home'])
        progress.home = source_home

        def before_close(profile_id, thread_id):
            expected = close_expectation
            if profile_id != expected.get('source') or thread_id != parent_id:
                raise RuntimeError('Unexpected administrative close was attempted.')
            recent_reads = {entry['thread_id'] for entry in report['audit'][expected['audit_start']:]
                if entry['profile_id'] == expected['target'] and entry['method'] == 'thread/read'}
            if not set(expected['scope']).issubset(recent_reads):
                raise RuntimeError('Target read-only preflight did not cover the source subtree before close.')
            if record_snapshot(source_home) != expected['record_snapshot']:
                raise RuntimeError('Canonical source records changed during the target read-only preflight.')
            report['checks']['대상 읽기 검증을 완료한 뒤 원본 종료 요청'] = True

        local_sources = [source_item for source_item in store.read()['sources']
                         if source_item['id'] in {'manager:' + profile['id'] for profile in profiles}]
        shared_catalog_path = None
        if runtime.get('capabilities', {}).get('shared_record_catalog'):
            # A private manifest for these new fixtures, never the real 1,476+
            # record index. Its membership will refresh after native file work.
            shared_catalog_path = record_catalog.build(fixture, local_sources, include_paginated=True)['path']
        for profile in profiles:
            env = environment_for(root, store, profile, runtime, bootstrap, provider_env,
                                  shared_catalog=shared_catalog_path)
            try:
                client = ManagedClient(bootstrap, env, workspace, progress)
            finally:
                env.clear()
            clients[profile['id']] = client
            kind = (client.request('account/read', {'refreshToken': False}, timeout=45).get('account') or {}).get('type')
            if kind != 'chatgpt':
                raise RuntimeError('An isolated execution runtime did not authenticate the selected ChatGPT account.')
            admin = AuditedAdmin(root, profile, report['audit'], before_close)
            admins[profile['id']] = admin
            identities[profile['id']] = admin.identities()
        report['identities_before'] = identities
        manager = HandoffManager(root, store, admin_factory=lambda profile: admins[profile['id']])
        source, target = profiles
        client_a, client_b = clients[source['id']], clients[target['id']]
        watch_membership = runtime.get('capabilities', {}).get('native_catalog_membership') is True
        if watch_membership:
            if shared_catalog_path is None or client_b.request('thread/list', {'limit': 100})['data']:
                raise RuntimeError('Common-list observation must start from the empty isolated target.')
        nonce = 'handoff-' + uuid4().hex
        (workspace / 'input.txt').write_text(nonce, encoding='utf-8')
        instructions = (f'The user explicitly authorizes native V2 delegation in {workspace}. '
            'Never read credentials or configuration outside this test workspace. '
            f'Use external_agents.spawn_agent with agent_type {binding["role_id"]}, task_name external_worker, '
            'fork_turns none, and no nested delegation. Delegate file and shell work to that one child. '
            'Never replace that child or provider on an error. Do not perform file or shell operations yourself. '
            'For each user request, dispatch the child task exactly once, then use only wait until completion. '
            'Do not send the child status pings, reminders, acknowledgments, or other messages. '
            'Only send a followup_task when a later user request explicitly asks for the next file change. '
            'Report failures accurately and wait for the child to finish before replying.')
        started = client_a.request('thread/start', {'model': 'gpt-6-astra', 'cwd': str(workspace),
            'approvalPolicy': 'never', 'sandbox': 'workspace-write', 'developerInstructions': instructions})
        parent_id = started['thread']['id']
        if started.get('model') != 'gpt-6-astra' or started.get('modelProvider') != 'openai':
            raise RuntimeError('The first parent runtime substituted a model or provider.')
        report['parent_thread_id'] = parent_id
        client_a.turn(parent_id,
            'Create exactly one external_worker DeepSeek child. It must read input.txt using a tool, '
            'write result.txt as exactly that text plus |A (UTF-8, no BOM and no trailing newline), '
            'and verify exact bytes using a shell assertion that exits 0. Wait for the child. Do not write the file yourself.', timeout=480)
        verify_output_bytes(workspace, (nonce + '|A').encode(), report, 'account_02_first_turn')
        routes = collect_routes(source_home, parent_id)
        children = [route for route in routes if route['id'] != parent_id]
        if len(children) != 1 or children[0]['provider'] != binding['runtime_provider_id']:
            raise RuntimeError('The parent did not create exactly one external child with the intended provider.')
        child_id = children[0]['id']
        report['child_thread_id'] = child_id
        report['execution_evidence'] = check_execution(source_home, parent_id, child_id, binding, 1)
        report['checks']['계정 02에서 Astra·DeepSeek max 실제 셸 작업'] = True
        peer_id = client_a.request('thread/start', {'model': 'gpt-6-astra', 'cwd': str(workspace),
            'approvalPolicy': 'never', 'sandbox': 'workspace-write',
            'developerInstructions': 'Reply briefly. Do not use tools or subagents.'})['thread']['id']
        client_a.turn(peer_id, 'Reply exactly PEER_READY.', timeout=120)
        peer_grant = authority.read(source_home, peer_id)
        report['peer_thread_id'] = peer_id

        # Verify common reads in working instances and retain coverage for the
        # older dedicated viewer. All manifests cover only this fresh test run.
        catalog = record_catalog.build(fixture, local_sources, include_paginated=True)
        mapped = [entry for entry in catalog['mapping'] if entry['thread_id'] == parent_id
                  and entry['source_store_id'] == 'manager:' + source['id']]
        if not catalog['complete'] or len(mapped) != 1:
            raise RuntimeError('The isolated native record catalog did not cover the fresh canonical parent.')
        if watch_membership:
            expected_ids = {entry['projection_thread_id'] for entry in catalog['mapping']}
            received = {}
            deadline = time.monotonic() + 20
            # Consume pushed notifications only. A repeated list/read request
            # must not be mistaken for automatic membership delivery.
            while set(received) != expected_ids:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError('The already-open target did not receive every new common-list entry.')
                message = client_b.next_message(remaining)
                thread = message.get('params', {}).get('thread', {})
                if message.get('method') != 'thread/started' or thread.get('id') not in expected_ids:
                    continue
                if thread['id'] in received or thread.get('canAcceptDirectInput') is not False or thread.get('path') is not None:
                    raise RuntimeError('Common-list delivery duplicated an entry or exposed a writable record.')
                received[thread['id']] = thread.get('parentThreadId')
            target_health = admins[target['id']].request('manager/maintenance/status', {})
            activity_fields = ('activeTurnCount', 'activeToolCount', 'activeChildCount',
                               'activeProcessCount', 'pendingMutationCount', 'pendingApprovalCount', 'queueUnknownCount')
            if loaded_ids(admins[target['id']]) or any(type(target_health.get(key)) is not int or target_health[key] != 0 for key in activity_fields):
                raise RuntimeError('Common-list membership activated work in the target account.')
            report['catalog_membership'] = {'initial_entries': 0, 'pushed_entries': len(received),
                'projection_parents': received, 'list_requests_after_refresh': 0,
                'target_activity': {key: target_health[key] for key in activity_fields}}
            report['checks']['열린 작업 프로필에 새 대화 목록 자동 전달·실행 없음'] = True
            if runtime.get('capabilities', {}).get('native_catalog_names') is True:
                projection_id = mapped[0]['projection_thread_id']
                manifest_before_name = Path(shared_catalog_path).read_bytes()
                name = '공통 대화 이름 자동 반영 시험'
                client_a.request('thread/name/set', {'threadId': parent_id, 'name': name})
                deadline = time.monotonic() + 20
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise RuntimeError('The unopened target record did not receive its new canonical name.')
                    message = client_b.next_message(remaining)
                    params = message.get('params', {})
                    if message.get('method') == 'thread/name/updated' and params.get('threadId') == projection_id:
                        if params.get('threadName') != name:
                            raise RuntimeError('Common-list name update did not match the canonical writer.')
                        break
                if Path(shared_catalog_path).read_bytes() != manifest_before_name or loaded_ids(admins[target['id']]):
                    raise RuntimeError('Name-only observation changed membership or activated a target actor.')
                named_rows = [row for row in list_catalog(local_sources)['conversations']
                    if row['thread_id'] == parent_id and row['source_store_id'] == 'manager:' + source['id']]
                if len(named_rows) != 1 or named_rows[0]['title'] != name:
                    raise RuntimeError('The manager task chooser did not expose the same canonical name.')
                report['catalog_name_update'] = {'thread_id': projection_id, 'thread_name': name,
                    'list_requests_after_rename': 0, 'record_opened': False,
                    'manifest_unchanged': True, 'loaded_target_actors': 0,
                    'manager_task_chooser_name_matches': True}
                report['checks']['열지 않은 공통 대화의 제목 자동 반영·목록 재조회 없음'] = True
        if shared_catalog_path is not None:
            projection_id = mapped[0]['projection_thread_id']
            before = record_snapshot(source_home)
            native = client_a.request('thread/list', {'limit': 100})['data']
            foreign = client_b.request('thread/list', {'limit': 100})['data']
            if (not any(row['id'] == parent_id for row in native)
                    or any(row['id'] == projection_id for row in native)
                    or not any(row['id'] == projection_id and row['canAcceptDirectInput'] is False for row in foreign)):
                raise RuntimeError('Working instances did not retain their canonical and shared record identities.')
            visible = client_b.request('thread/read', {'threadId': projection_id, 'includeTurns': True})['thread']
            if (visible['id'] != projection_id or visible['canAcceptDirectInput'] is not False
                    or record_snapshot(source_home) != before):
                raise RuntimeError('Working instance common history modified or lost its canonical source.')
            report['checks']['작업 인스턴스의 공통 목록·자기 대화 입력·외부 기록 읽기'] = True
        viewer, _ = prepare_profile(store, accounts[0], fingerprints[0], registry, model['id'], viewer=True)
        profiles.append(viewer)
        before = record_snapshot(source_home)
        env = environment_for(root, store, viewer, runtime, bootstrap, {}, catalog=catalog['path'])
        try:
            viewer_client = ManagedClient(bootstrap, env, workspace, progress)
        finally:
            env.clear()
        clients[viewer['id']] = viewer_client
        viewer_account = viewer_client.request('account/read', {'refreshToken': False}, timeout=45)
        if (viewer_account.get('account') or {}).get('type') != 'chatgpt':
            raise RuntimeError('The read-only viewer did not complete access-token account binding.')
        viewer_health = AdminClient(root, viewer['id'], viewer['generation']).request('manager/maintenance/status', {})
        if not viewer_health.get('accountReady') or not viewer_health.get('initialized'):
            raise RuntimeError('The read-only viewer RuntimeProxy did not become ready.')
        report['checks']['읽기 전용 viewer의 access-token 인증·초기화'] = True
        projection_id = mapped[0]['projection_thread_id']
        listing = viewer_client.request('thread/list', {'limit': 100})
        if not any(item.get('id') == projection_id for item in listing.get('data', [])):
            raise RuntimeError('The native runtime thread list omitted the projected record.')
        visible = viewer_client.request('thread/read', {'threadId': projection_id, 'includeTurns': True})
        if visible.get('thread', {}).get('id') != projection_id or record_snapshot(source_home) != before:
            raise RuntimeError('Native viewer identity or canonical source immutability check failed.')
        report['checks']['원본 런타임의 목록·대화 읽기와 원본 기록 불변'] = True
        report['catalog'] = {'entries': catalog['entries'], 'projection_thread_id': projection_id,
                             'record_file_count': len(before), 'source_unchanged': True}
        watch_updates = runtime.get('capabilities', {}).get('native_catalog_updates') is True
        if watch_updates:
            viewer_client.request('thread/resume', {'threadId': projection_id, 'excludeTurns': True})
        else:
            viewer_client.close()
            clients.pop(viewer['id'])

        reference = {'thread_id': parent_id, 'host_id': 'local', 'source_store_id': 'manager:' + source['id']}
        for index, (old, new, resumed_client, suffix) in enumerate(
                ((source, target, client_b, '|B'), (target, source, client_a, '|A2')), 1):
            preview = manager.preview(reference, new['id'])
            # Completion notifications can precede mailbox/actor cleanup. Retry
            # only this read-only preflight, never a close or transfer mutation.
            idle_deadline = time.monotonic() + 30
            while (preview.get('code') == 'source_busy'
                   and time.monotonic() < idle_deadline):
                time.sleep(.5)
                preview = manager.preview(reference, new['id'])
            report['last_handoff_preview'] = preview
            if preview.get('status') != 'ready' or preview.get('requires_handoff') is not True:
                raise RuntimeError('Handoff preflight blocked: ' + str(preview.get('code', 'unverified')))
            scope = preview['thread_ids']
            if set(scope) != {parent_id, child_id} or peer_id in scope:
                raise RuntimeError('The strict handoff scope did not match the parent and same child only.')
            close_expectation.clear()
            close_expectation.update(source=old['id'], target=new['id'], scope=scope,
                audit_start=len(report['audit']), record_snapshot=record_snapshot(source_home))
            result = manager.continue_conversation(reference, new['id'])
            report['transfers'].append(result)
            if (result.get('writer_release_verified') is not True or result.get('binding_reloaded') is not True
                    or result.get('owner_profile_id') != new['id']):
                raise RuntimeError('Handoff did not produce verified release and activation: ' + str(result.get('code', 'unverified')))
            if {parent_id, child_id}.intersection(loaded_ids(admins[old['id']])):
                raise RuntimeError('The source still exposes a loaded transferred actor.')
            resumed = resumed_client.request('thread/resume', {'threadId': parent_id, 'cwd': str(workspace)})
            if resumed['thread']['id'] != parent_id or resumed.get('model') != 'gpt-6-astra' or resumed.get('modelProvider') != 'openai':
                raise RuntimeError('The target did not resume the same canonical Astra parent.')
            resumed_client.turn(parent_id,
                f'Send followup_task to the SAME existing external_worker DeepSeek child ({child_id}); do not spawn a new agent. '
                f'Ask it to append exactly {suffix} to result.txt with no newline/BOM and verify exact bytes with a shell assertion exiting 0. '
                'Send that followup exactly once, then use only wait until the child finishes. '
                'Do not send status pings, reminders, acknowledgments, or additional followups to the child. '
                'Report the result after completion. Do not edit the file yourself.', timeout=360)
            expected = nonce + '|A|B' + ('|A2' if index == 2 else '')
            verify_output_bytes(workspace, expected.encode(), report, 'handoff_' + str(index))
            report['execution_evidence'] = check_execution(source_home, parent_id, child_id, binding, index + 1)
            if watch_updates:
                canonical_completions = [event['params']['turn']['id'] for event in resumed_client.events
                    if event.get('method') == 'turn/completed' and event.get('params', {}).get('threadId') == parent_id]
                if not canonical_completions:
                    raise RuntimeError('The writer did not report the canonical turn completion.')
                completed_turn_id = canonical_completions[-1]
                replies = {event['params']['item']['id']: event['params']['item']['text']
                    for event in resumed_client.events if event.get('method') == 'item/completed'
                    and event.get('params', {}).get('threadId') == parent_id
                    and event.get('params', {}).get('turnId') == completed_turn_id
                    and event.get('params', {}).get('item', {}).get('type') == 'agentMessage'}
                if not replies:
                    raise RuntimeError('The writer did not report any persisted assistant reply for comparison.')
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    viewer_client.request('thread/read', {'threadId': projection_id, 'includeTurns': False})
                    completed = [event for event in viewer_client.events if event.get('method') == 'turn/completed'
                                 and event.get('params', {}).get('threadId') == projection_id
                                 and event.get('params', {}).get('turn', {}).get('id') == completed_turn_id]
                    received_replies = {event['params']['item']['id']: event['params']['item']['text']
                        for event in viewer_client.events if event.get('method') == 'item/completed'
                        and event.get('params', {}).get('threadId') == projection_id
                        and event.get('params', {}).get('turnId') == completed_turn_id
                        and event.get('params', {}).get('item', {}).get('type') == 'agentMessage'}
                    if completed and received_replies == replies:
                        break
                    time.sleep(.25)
                else:
                    report['catalog_update_failure'] = {
                        'turn_id': completed_turn_id,
                        'expected_reply_item_ids': sorted(replies),
                        'received_reply_item_ids': sorted(received_replies),
                        'completion_events': len(completed),
                        'recent_status': [event['params'].get('status') for event in viewer_client.events
                            if event.get('method') == 'thread/status/changed'
                            and event.get('params', {}).get('threadId') == projection_id][-5:],
                        'diagnostics': [progress.clean(line)[:1200] for line in viewer_client.stderr
                            if 'common record update' in line][-5:],
                    }
                    raise RuntimeError('The already-open common record did not receive the same completed turn and assistant replies.')
                health = AdminClient(root, viewer['id'], viewer['generation']).request('manager/maintenance/status', {})
                activity_fields = ('activeTurnCount', 'activeToolCount', 'activeChildCount',
                                   'activeProcessCount', 'pendingMutationCount', 'pendingApprovalCount', 'queueUnknownCount')
                if any(type(health.get(field)) is not int or health[field] != 0 for field in activity_fields):
                    raise RuntimeError('Foreign history observation was mistaken for work in the viewer account.')
                report.setdefault('catalog_updates', []).append({'turn_id': completed_turn_id,
                    'completion_events': len(completed), 'matching_reply_items': len(replies),
                    'viewer_activity': {field: health[field] for field in activity_fields}})
                report['checks']['인계 ' + str(index) + ': 열린 공통 기록에 새 완료 이벤트 수신'] = True
            if peer_id not in loaded_ids(admins[source['id']]) or authority.read(source_home, peer_id) != peer_grant:
                raise RuntimeError('An unrelated source peer was unloaded or its authority changed.')
            answers = client_a.turn(peer_id, 'Reply exactly PEER_STILL_READY. Do not use tools.', timeout=120)
            if not any('PEER_STILL_READY' in answer for answer in answers):
                raise RuntimeError('The unrelated source peer did not respond after handoff.')
            for profile in (source, target):
                if admins[profile['id']].identities() != identities[profile['id']]:
                    raise RuntimeError('Handoff restarted a whole runtime or changed its process identity.')
            report['checks']['인계 ' + str(index) + ': 같은 자식·원본 기록·peer 유지·프로세스 재시작 없음'] = True
            if 'catalog_name_update' in report:
                named_rows = [row for row in list_catalog(local_sources)['conversations']
                    if row['thread_id'] == parent_id and row['source_store_id'] == 'manager:' + source['id']]
                if len(named_rows) != 1 or named_rows[0]['title'] != report['catalog_name_update']['thread_name']:
                    raise RuntimeError('The canonical name changed while continuing the same task in another profile.')
                report.setdefault('catalog_name_preserved_after_turns', []).append(index)
            write_report(output, report, progress)
        target_routes = collect_routes(Path(target['home']), parent_id)
        if target_routes:
            raise RuntimeError('A duplicate parent or child rollout was written into the target profile HOME.')
        report['checks']['대상 HOME에 대화 복사본 생성 없음'] = True
        report['checks']['DeepSeek 전체 실행 max·실제 셸 성공·같은 자식 ID'] = True
        report['status'] = 'PASS'
    except Exception as error:
        # Model payloads and raw provider failures are not embedded in this report.
        report.update(status='FAIL', error=progress.clean(str(error))[:1600])
    finally:
        for client in reversed(tuple(clients.values())):
            try:
                client.close()
            except Exception:
                report.update(status='FAIL', cleanup_error='An owned headless fixture runtime did not close cleanly.')
        provider_env.clear()
        for profile in profiles:
            if (Path(profile['home']) / 'auth.json').exists():
                report.update(status='FAIL', error='An isolated fixture HOME unexpectedly persisted authentication.')
        report['checks']['테스트 HOME에 로그인 파일 저장 없음'] = all(not (Path(p['home']) / 'auth.json').exists() for p in profiles)
        report['finished_at'] = time.strftime('%Y-%m-%dT%H:%M:%S%z')
        report['profiles'] = [{'id': p['id'], 'alias': p['alias'], 'home': p['home'], 'view_only': p['view_only']} for p in profiles]
        write_report(output, report, progress)
        progress.close()
    print(json.dumps({'status': report['status'], 'report': str(output / 'report.json'),
                      'error': report.get('error')}, ensure_ascii=False), flush=True)
    return 0 if report['status'] == 'PASS' else 1


def main(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true', help='Explicitly run the fresh 02/04 live model test after validated runtime publication.')
    parser.add_argument('--check-release', action='store_true', help='Only verify the published runtime and bootstrap; no account reads or processes.')
    parser.add_argument('--candidate-manifest', type=Path, help='Test an immutable candidate without changing the active runtime pointer.')
    args = parser.parse_args(arguments)
    if args.check_release:
        try:
            runtime, bootstrap, digest = checked_release(ROOT, args.candidate_manifest)
            print(json.dumps({'status': 'READY', 'runtime': runtime['runtime'], 'sha256': runtime['sha256'],
                              'bootstrap': str(bootstrap), 'bootstrap_sha256': digest}))
            return 0
        except (OSError, ValueError, RuntimeError, KeyError) as error:
            print(json.dumps({'status': 'WAITING_FOR_TESTED_RUNTIME', 'reason': str(error)}))
            return 2
    if not args.execute:
        print(json.dumps({'status': 'NOT_RUN', 'accounts': list(ALLOWED_ALIASES),
            'required_capabilities': list(REQUIRED_CAPABILITIES), 'next': '--check-release, then explicit --execute',
            'scope': 'new isolated local records; A -> B -> A; same external child; no GUI'}, ensure_ascii=False))
        return 0
    try:
        return run(candidate_manifest=args.candidate_manifest)
    except (OSError, ValueError, RuntimeError, KeyError) as error:
        print(json.dumps({'status': 'BLOCKED_BEFORE_START', 'reason': str(error)}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
