"""02 -> 04 -> 02 on remote-dev, in fresh unlisted profiles and workspace.

No GUI, stock CLI change, remote login file, or existing conversation mutation.
The default prints the plan; --execute runs the authorized integration test.
"""
import argparse
import hashlib
import inspect
import json
import os
from pathlib import Path, PurePosixPath
import shlex
import sys
import time
from uuid import uuid4

from desktop_launch import find_app
from manager_core.accounts import Accounts
from manager_core.providers import ProviderRegistry
from manager_core.proxy_auth import account_fingerprint, read_existing_tokens
from manager_core.remote import RemoteManager
from manager_core.remote_handoff import RemoteHandoffManager
from manager_core.runtime_admin import AdminClient
from manager_core.ssh_runtime_control import endpoint_id
from manager_core.ssh_shim import native_bodies, native_command, prepare_environment
from manager_core.store import Store
from progress import Progress
from test_manager_handoff_live import isolated_store, select_accounts
from test_manager_ssh_live import SshManagedClient, remote_python
import test_manager_live as execution

ROOT = Path(__file__).resolve().parents[1]


def selected_accounts(root, progress):
    available = Accounts(root).list()
    profiles = Store(root).read()['profiles']
    for alias in ('02', '04'):
        native = [p for p in profiles if p.get('auth_mode') == 'native' and p.get('alias') == alias and not p.get('removed_at')]
        if native:
            if len(native) != 1 or native[0].get('login_state') != 'signed_in':
                raise RuntimeError('A test account has not finished Windows login.')
            p = native[0]
            available = [a for a in available if a['alias'] != alias]
            available.append(dict(id=p['id'], alias=alias, home=p['home'], expected_fingerprint=p['account_fingerprint']))
    result = select_accounts(available)
    for account in result:
        tokens = read_existing_tokens(account['home'])
        progress.add_secret(tokens.access_token)
        progress.add_secret(tokens.account_id)
        fingerprint = account_fingerprint(tokens.account_id)
        if account.get('expected_fingerprint', fingerprint) != fingerprint:
            raise RuntimeError('Windows login identity changed.')
        account['fingerprint'] = fingerprint
    if result[0]['fingerprint'] == result[1]['fingerprint']:
        raise RuntimeError('Two distinct authenticated accounts required.')
    return result


def connect(root, remote, profile, account, binding, progress, *, client_factory=SshManagedClient):
    published = json.loads((root / 'artifacts/manager/current.json').read_text(encoding='utf-8-sig'))
    executable = Path(published['ssh_proxy']).resolve(strict=True)
    if not executable.is_relative_to(root / 'artifacts/manager/releases'):
        raise RuntimeError('Published native SSH bootstrap required.')
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith('CODEX_') and k != 'ELECTRON_RUN_AS_NODE'}
    env.update(CODEX_MANAGER_ROOT=str(root), CODEX_MANAGER_PYTHON=sys.executable,
               CODEX_MANAGER_SSH_AUTH_SOURCE=account['home'],
               CODEX_MANAGER_EXPECTED_ACCOUNT_FINGERPRINT=account['fingerprint'],
               CODEX_MANAGER_GENERATION=profile['generation'])
    from manager_core.ssh_compatibility import fingerprint
    installed_app = find_app()
    env = prepare_environment(root, profile['id'], [binding], env, ssh_proxy=executable,
        app_version=installed_app['Version'], app_source_sha256=fingerprint(installed_app['executable']))
    start = remote._run(binding['alias'], shlex.join([binding['remote_python'], binding['remote_launcher'],
                         binding['revision'], 'native-start']), timeout=40)
    if start.returncode: raise RuntimeError('The fresh remote listener did not start.')
    marker = os.urandom(8)
    args = ['-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', binding['alias'],
            native_command(native_bodies()['native-proxy'], marker)]
    client = client_factory(executable, args, env, marker, progress,
                              notification_opt_outs=['thread/closed', 'process/exited'])
    if (client.request('account/read', {'refreshToken': False}).get('account') or {}).get('type') != 'chatgpt':
        client.close()
        raise RuntimeError('The remote runtime did not bind the selected ChatGPT account.')
    return client


def evidence(remote, binding, parent, workspace):
    # Reuse the bounded, allowlisted parser. Only metadata and file hashes cross
    # SSH; no transcript, credentials, command text, or reasoning is downloaded.
    source = 'import json,re,sys,hashlib\nfrom pathlib import Path\n'
    for name in ('_AGENT_CONTROL_TOOLS', '_NON_FILE_ITEMS', '_NON_TOOL_RESPONSE_ITEMS'):
        source += name + '=' + repr(getattr(execution, name)) + '\n'
    source += inspect.getsource(execution.collect_execution_evidence)
    source += '''
home=Path(sys.argv[1]); workspace=Path(sys.argv[3])
assert home.resolve()==home and home.parent.name==sys.argv[4] and home.name=='codex'
data=collect_execution_evidence(home,sys.argv[2])
p=workspace/'result.txt'
data['result_sha256']=hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None
data['auth_file_present']=(home/'auth.json').exists()
data['session_hashes']={str(p.relative_to(home)):hashlib.sha256(p.read_bytes()).hexdigest() for p in (home/'sessions').rglob('*.jsonl')}
print(json.dumps(data))
'''
    return remote_python(remote, binding, source, [binding['remote_profile_home'], parent, workspace, binding['profile_id']])


def run(root=ROOT):
    run_id = 'manager-ssh-handoff-' + time.strftime('%Y%m%d-%H%M%S') + '-' + uuid4().hex[:6]
    output = root / 'artifacts/results' / run_id
    progress = Progress(output)
    store = isolated_store(root, run_id)
    remote = RemoteManager(root)
    report = dict(run_id=run_id, host='remote-dev', status='RUNNING', checks={}, transfers=[],
                  fixture_store=str(store.path), started_at=time.strftime('%Y-%m-%dT%H:%M:%S%z'))
    clients, profiles, bindings = {}, [], []
    try:
        accounts = selected_accounts(root, progress)
        report['accounts'] = [{k: a[k] for k in ('alias', 'fingerprint')} for a in accounts]
        registry = ProviderRegistry(root)
        models = [m for m in registry.list()['models'] if m['wire_model_id'] == 'deepseek-flash']
        if len(models) != 1: raise RuntimeError('Exactly one DeepSeek Flash binding required.')
        model = models[0]
        for secret in registry.environment([model['id']]).values(): progress.add_secret(secret)
        for account in accounts:
            profile = store.add_profile('SSH-test-' + account['alias'])
            Path(profile['home']).mkdir(parents=True)
            progress.emit('새 시험 프로필에 remote-dev 런타임을 준비합니다: 계정 ' + account['alias'])
            binding = remote.prepare('remote-dev', profile['id'], profile['home'], [model['id']])
            if not binding.get('prepared'): raise RuntimeError('Fresh remote profile preparation failed.')
            def save(data):
                p = store.profile(profile['id'], data)
                p.update(generation=str(uuid4()), remote_bindings=[binding], account_fingerprint=account['fingerprint'])
                data['sources'].append(dict(id='manager:' + p['id'], host_id='ssh:remote-dev',
                    home=binding['remote_profile_home'], alias=p['alias']))
                return p
            profile = store.mutate(save)
            profiles.append(profile); bindings.append(binding)
            clients[profile['id']] = connect(root, remote, profile, account, binding, progress)
            progress.write_report(report)
        source, target = profiles
        client_a, client_b = (clients[p['id']] for p in profiles)
        manager = RemoteHandoffManager(root, store, remote, 'remote-dev')
        report['profiles'] = [dict(id=p['id'], generation=p['generation']) for p in profiles]
        report['bindings'] = bindings
        role = registry.render_for_host(bindings[0]['remote_profile_home'], True, [model['id']])['bindings'][0]
        if role['reasoning_effort'] != 'max': raise RuntimeError('Flash must use max.')
        report['model_binding'] = {k: role[k] for k in ('role_id', 'runtime_provider_id', 'wire_model_id', 'reasoning_effort')}
        workspace = str(PurePosixPath(bindings[0]['remote_launcher']).parents[2] / 'live-workspaces' / run_id)
        report['workspace'] = workspace
        nonce = 'ssh-handoff-' + uuid4().hex
        remote_python(remote, bindings[0], 'import json,pathlib,subprocess,sys\np=pathlib.Path(sys.argv[1]);p.mkdir(parents=True,exist_ok=False)\n(p/"input.txt").write_text(sys.argv[2],encoding="utf-8")\nsubprocess.run(["git","init","-q",str(p)],check=True)\nprint(json.dumps({"created":True}))', [workspace, nonce])
        instructions = (f'The user authorizes native V2 delegation inside {workspace}. '
            'Never read credentials or configuration outside this workspace. Delegate all file work to one child. '
            f'Use external_agents.spawn_agent with agent_type {role["role_id"]}, task_name external_worker, '
            'fork_turns none, no nested delegation. Keep the same child for follow-up tasks. '
            'Do not substitute a model or child on failure, and never perform file or shell work yourself.')
        parent = client_a.request('thread/start', dict(model='gpt-6-astra', cwd=workspace, approvalPolicy='never',
            sandbox='workspace-write', developerInstructions=instructions))['thread']['id']
        report['parent_thread_id'] = parent
        progress.emit('계정 02의 Astra가 DeepSeek Flash max 자식에게 첫 파일 작업을 맡깁니다.')
        client_a.turn(parent, 'Create one external_worker child. Ask it to read input.txt with a tool, write result.txt '
            'as exactly that text plus |A (UTF-8, no BOM, no newline), and verify exact bytes using a shell assertion. Wait for it.', timeout=480)
        first = evidence(remote, bindings[0], parent, workspace)
        if first['result_sha256'] != hashlib.sha256((nonce + '|A').encode()).hexdigest():
            raise RuntimeError('Initial DeepSeek file result mismatch.')
        rows = first['threads']
        child_rows = [r for r in rows if r['thread_id'] != parent]
        if len(child_rows) != 1: raise RuntimeError('Exactly one external child required.')
        child = child_rows[0]['thread_id']
        report['child_thread_id'] = child
        report['checks']['initial_remote_deepseek_file_work'] = True
        # A second loaded task must remain available throughout both transfers.
        peer = client_a.request('thread/start', dict(model='gpt-6-astra', cwd=workspace, approvalPolicy='never',
            sandbox='workspace-write', developerInstructions='Reply briefly without tools or subagents.'))['thread']['id']
        client_a.turn(peer, 'Reply exactly PEER_READY.', timeout=150)
        report['peer_thread_id'] = peer
        reference = dict(thread_id=parent, host_id='ssh:remote-dev', source_store_id='manager:' + source['id'])
        expected = nonce + '|A'
        for target_profile, next_client, suffix in ((target, client_b, '|B'), (source, client_a, '|A2')):
            progress.emit('같은 부모·DeepSeek 자식 기록을 선택 계정으로 인계합니다: ' + target_profile['alias'])
            preview = manager.preview(reference, target_profile['id'])
            report.setdefault('previews', []).append(preview)
            progress.write_report(report)
            if preview.get('status') != 'ready' or set(preview.get('thread_ids', [])) != {parent, child}:
                raise RuntimeError('SSH handoff preview blocked: ' + json.dumps(preview, ensure_ascii=False))
            before = evidence(remote, bindings[0], parent, workspace)['session_hashes']
            transfer = manager.continue_conversation(reference, target_profile['id'])
            report['transfers'].append(transfer); progress.write_report(report)
            if not transfer.get('writer_release_verified') or not transfer.get('binding_reloaded'):
                raise RuntimeError('SSH handoff incomplete: ' + json.dumps(transfer, ensure_ascii=False))
            after = evidence(remote, bindings[0], parent, workspace)['session_hashes']
            if before != after: raise RuntimeError('Canonical record bytes changed during administrative handoff.')
            # A raw app-server client must submit the selected task permission
            # settings on resume. The target's default is intentionally read-only.
            # Keep the same authorized workspace boundary as the initial turn.
            resumed = next_client.request('thread/resume', {'threadId': parent, 'cwd': workspace,
                'approvalPolicy': 'never', 'sandbox': 'workspace-write'})
            if resumed['thread']['id'] != parent: raise RuntimeError('Parent ID changed during resume.')
            next_client.turn(parent, 'Send a followup_task to the SAME external_worker child (ID ' + child +
                '), do not spawn another child. Ask it to append exactly ' + suffix + ' to result.txt using a shell tool, '
                'verify the resulting bytes with a shell assertion, and wait for completion. Do no file work yourself.', timeout=480)
            expected += suffix
            current = evidence(remote, bindings[0], parent, workspace)
            if current['result_sha256'] != hashlib.sha256(expected.encode()).hexdigest():
                raise RuntimeError('Transferred same-child result mismatch.')
            report['execution_evidence'] = current
            admin_a = AdminClient(root, endpoint_id(source['id'], 'remote-dev'), source['generation'])
            if peer not in manager._loaded(admin_a): raise RuntimeError('Independent peer was unloaded.')
            progress.write_report(report)
        final = report['execution_evidence']
        records = final['threads']
        c = [r for r in records if r['thread_id'] == child]
        p = [r for r in records if r['thread_id'] == parent]
        if len(records) != 2 or len(c) != 1 or len(p) != 1: raise RuntimeError('Unexpected delegated child after handoff.')
        if c[0]['provider'] != role['runtime_provider_id'] or len(c[0]['turns']) != 3:
            raise RuntimeError('Same external provider history missing.')
        if any(t.get('model') != 'deepseek-flash' or t.get('effort') != 'max' or not t.get('completed')
               or t['successful_commands'] < 1 for t in c[0]['turns']):
            raise RuntimeError('Flash max or actual successful file tool evidence missing.')
        if any(p[0][k] for k in ('other_tool_calls', 'command_items', 'file_change_items', 'unknown_tool_items')):
            raise RuntimeError('Parent performed file work.')
        if final['auth_file_present']: raise RuntimeError('Remote source login file unexpectedly created.')
        target_records = evidence(remote, bindings[1], parent, workspace)
        if target_records['session_hashes'] or target_records['auth_file_present']:
            raise RuntimeError('Target copied source history or stored a login file.')
        client_a.turn(peer, 'Reply exactly PEER_STILL_AVAILABLE.', timeout=150)
        report['checks'].update(two_distinct_accounts=True, roundtrip_same_parent_child=True,
            same_selected_workspace_permission_on_resume=True,
            flash_max_all_three_turns=True, actual_child_commands_all_turns=True,
            parent_did_no_file_work=True, canonical_records_unchanged_during_handoff=True,
            target_no_record_copy=True, remote_auth_files_absent=True, independent_peer_available=True)
        report['status'] = 'PASS'
    except Exception as error:
        report.update(status='FAIL', error=progress.clean(str(error))[:5000])
        progress.emit('원격 인계 시험 오류: ' + report['error'])
    finally:
        report['cleanup'] = {}
        for pid, client in clients.items():
            try: report['cleanup'][pid] = client.close()
            except Exception as error:
                report.update(status='FAIL', cleanup_error=progress.clean(str(error))[:1000])
        report['remote_daemon_exit_verified'] = False
        report['finished_at'] = time.strftime('%Y-%m-%dT%H:%M:%S%z')
        progress.write_report(report)
        progress.emit('최종 상태: ' + report['status'])
        progress.close()
    print(json.dumps(dict(status=report['status'], report=str(output / 'report.json'), error=report.get('error')), ensure_ascii=False), flush=True)
    return 0 if report['status'] == 'PASS' else 1


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser()
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    if not args.execute:
        print('Plan: fresh remote-dev profiles, verified accounts 02/04, Astra + Flash max, canonical roundtrip. No GUI.')
    else:
        raise SystemExit(run())
