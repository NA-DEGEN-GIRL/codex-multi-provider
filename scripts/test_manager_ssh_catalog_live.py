"""Native SSH common-history observation in fresh profiles; no account handoff.

Requires a separately staged catalog runtime. Uses 04 for actual Astra/Flash max
file work and 02 only to observe its record through the native SSH connection.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path, PurePosixPath
import sys
import time
from uuid import uuid4

from manager_core.catalog_origin import CatalogOrigins
from manager_core.providers import ProviderRegistry
from manager_core.remote import RemoteManager
from manager_core.runtime_admin import AdminClient
from manager_core.ssh_runtime_control import endpoint_id
from progress import Progress
from test_manager_handoff_live import isolated_store
from test_manager_ssh_handoff_live import connect, evidence, selected_accounts
from test_manager_ssh_live import SshManagedClient, remote_python

ROOT = Path(__file__).resolve().parents[1]
ACTIVITY_FIELDS = ('activeTurnCount', 'activeToolCount', 'activeChildCount',
                   'activeProcessCount', 'pendingMutationCount', 'pendingApprovalCount', 'queueUnknownCount')


class ObservingClient(SshManagedClient):
    def __init__(self, *args, **kwargs):
        self.origins = CatalogOrigins()
        self.notifications = []
        self.methods = Counter()
        super().__init__(*args, **kwargs)

    def send(self, message):
        if 'id' in message and message.get('method'):
            self.methods[message['method']] += 1
        return super().send(message)

    def next_message(self, timeout):
        message = super().next_message(timeout)
        result = message.get('result', {})
        params = message.get('params', {})
        if message.get('method') == 'thread/started':
            self.origins.observe_thread(params['thread'])
        if isinstance(result, dict):
            if isinstance(result.get('thread'), dict):
                self.origins.observe_thread(result['thread'])
            if isinstance(result.get('data'), list):
                for row in result['data']:
                    if isinstance(row, dict) and 'id' in row:
                        self.origins.observe_thread(row)
        if message.get('method') in ('thread/started', 'thread/name/updated', 'turn/completed', 'item/completed'):
            self.notifications.append(message)
            if len(self.notifications) > 10000:
                raise RuntimeError('Test notification bound exceeded.')
        for identity in list(self.active):
            if self.origins(identity):
                self.active.pop(identity)
        return message

    def wait_for(self, predicate, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            found = next((message for message in self.notifications if predicate(message)), None)
            if found is not None:
                return found
            try:
                self.next_message(min(0.5, deadline - time.monotonic()))
            except TimeoutError:
                pass
        raise RuntimeError('Expected native catalog notification did not arrive.')


def source_catalog_hash(remote, binding):
    return remote_python(remote, binding, '''
import hashlib,json,sys
from pathlib import Path
profile=Path(sys.argv[1])
assert profile.name=='codex' and profile.parent.parent.name=='profiles'
path=profile.parent.parent.parent/'catalog-sources.json'
assert path.resolve()==path and path.stat().st_size<=4*1024*1024
print(json.dumps({'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}))
''', [binding['remote_profile_home']])['sha256']


def run(artifact_root):
    run_id = 'manager-ssh-catalog-' + time.strftime('%Y%m%d-%H%M%S') + '-' + uuid4().hex[:6]
    output = ROOT / 'artifacts/results' / run_id
    progress = Progress(output)
    store = isolated_store(ROOT, run_id)
    registry = ProviderRegistry(ROOT)
    remote = RemoteManager(artifact_root, registry=registry)
    report = dict(run_id=run_id, status='RUNNING', checks={}, host='remote-dev',
                  fixture_store=str(store.path), started_at=time.strftime('%Y-%m-%dT%H:%M:%S%z'))
    clients, profiles, bindings = [], [], []
    try:
        candidate = remote._artifact('linux', 'x86_64')
        if candidate.get('source_catalog_present') is not True:
            raise RuntimeError('The staged runtime does not support source catalogs.')
        report['bundle_id'] = candidate['bundle_id']
        accounts = sorted(selected_accounts(ROOT, progress), key=lambda account: account['alias'] != '04')
        report['accounts'] = [{'alias': a['alias'], 'fingerprint': a['fingerprint']} for a in accounts]
        models = [m for m in registry.list()['models'] if m['wire_model_id'] == 'deepseek-flash']
        if len(models) != 1:
            raise RuntimeError('Exactly one DeepSeek Flash binding required.')
        model = models[0]
        for secret in registry.environment([model['id']]).values():
            progress.add_secret(secret)
        for index, account in enumerate(accounts):
            profile = store.add_profile('SSH-catalog-test-' + account['alias'])
            Path(profile['home']).mkdir(parents=True)
            progress.emit('공통 대화 시험 프로필 준비: ' + account['alias'])
            binding = remote.prepare('remote-dev', profile['id'], profile['home'], [model['id']] if index == 0 else [])
            if not binding.get('prepared'):
                raise RuntimeError('Fresh native SSH profile preparation failed.')
            def save(data):
                current = store.profile(profile['id'], data)
                current.update(generation=str(uuid4()), remote_bindings=[binding], account_fingerprint=account['fingerprint'])
                return current
            profile = store.mutate(save)
            profiles.append(profile)
            bindings.append(binding)
            clients.append(connect(ROOT, remote, profile, account, binding, progress, client_factory=ObservingClient))
            report.update(profiles=[{'id': p['id'], 'generation': p['generation']} for p in profiles], bindings=bindings)
            progress.write_report(report)
        owner, reader = clients
        reader.request('thread/list', {'limit': 100})
        initial_catalog_hash = source_catalog_hash(remote, bindings[0])
        role = registry.render_for_host(bindings[0]['remote_profile_home'], True, [model['id']])['bindings'][0]
        if role['reasoning_effort'] != 'max':
            raise RuntimeError('Flash must use max.')
        report['model_binding'] = {k: role[k] for k in ('role_id', 'runtime_provider_id', 'wire_model_id', 'reasoning_effort')}
        workspace = str(PurePosixPath(bindings[0]['remote_launcher']).parents[2] / 'live-workspaces' / run_id)
        nonce = 'ssh-catalog-' + uuid4().hex
        remote_python(remote, bindings[0], '''
import json,pathlib,subprocess,sys
path=pathlib.Path(sys.argv[1]);path.mkdir(parents=True,exist_ok=False)
(path/'input.txt').write_text(sys.argv[2],encoding='utf-8')
subprocess.run(['git','init','-q',str(path)],check=True)
print(json.dumps({'created':True}))
''', [workspace, nonce])
        report['workspace'] = workspace
        instructions = (f'The user authorizes native V2 delegation inside {workspace}. '
            'Never read credentials or configuration outside this workspace. Delegate all file work to one child. '
            f'Use external_agents.spawn_agent with agent_type {role["role_id"]}, task_name external_worker, '
            'fork_turns none, no nested delegation. Keep the same child for follow-up tasks. '
            'Do not substitute a model or child on failure, and never perform file or shell work yourself.')
        parent = owner.request('thread/start', dict(model='gpt-6-astra', cwd=workspace,
            approvalPolicy='never', sandbox='workspace-write', developerInstructions=instructions))['thread']['id']
        report['parent_thread_id'] = parent
        progress.emit('04의 Astra와 Flash max로 파일 작업을 실행하고 02의 목록 반영을 확인합니다.')
        owner.turn(parent, 'Create one external_worker child. Ask it to read input.txt with a tool, '
            'write result.txt as exactly that text plus |A (UTF-8, no BOM, no newline), '
            'and verify exact bytes using a shell assertion. Wait for it.', timeout=480)
        started = reader.wait_for(lambda message: message['method'] == 'thread/started'
            and ((message['params']['thread'].get('extra') or {}).get('managedRecord') or {}).get('canonicalThreadId') == parent)
        projected = started['params']['thread']
        projection = projected['id']
        if projection == parent or projected.get('canAcceptDirectInput') is not False or projected.get('path') is not None:
            raise RuntimeError('Native common list did not identify a passive projection.')
        report['projection_thread_id'] = projection
        report['checks']['new_record_pushed_without_relisting'] = True
        first = evidence(remote, bindings[0], parent, workspace)
        children = [row for row in first['threads'] if row['thread_id'] != parent]
        if len(children) != 1 or first['result_sha256'] != hashlib.sha256((nonce + '|A').encode()).hexdigest():
            raise RuntimeError('Initial external child or file result mismatch.')
        child = children[0]['thread_id']
        report['child_thread_id'] = child
        title = 'SSH 공통 대화 자동 갱신 ' + run_id[-6:]
        owner.request('thread/name/set', {'threadId': parent, 'name': title})
        reader.wait_for(lambda message: message['method'] == 'thread/name/updated'
            and message['params'].get('threadId') == projection and message['params'].get('threadName') == title)
        report['checks']['unopened_title_pushed_without_relisting'] = True
        loaded = reader.request('thread/loaded/list', {})['data']
        if loaded:
            raise RuntimeError('Listing a foreign record loaded execution actors.')
        before = evidence(remote, bindings[0], parent, workspace)['session_hashes']
        viewed = reader.request('thread/resume', {'threadId': projection, 'excludeTurns': True})
        if viewed['thread'].get('extra') != projected.get('extra') or viewed['thread'].get('name') != title:
            raise RuntimeError('Read-only opening lost canonical origin or current title.')
        if evidence(remote, bindings[0], parent, workspace)['session_hashes'] != before:
            raise RuntimeError('Viewing changed canonical record bytes.')
        report['checks']['viewing_preserves_canonical_records'] = True
        progress.emit('같은 Flash 자식의 후속 작업이 이미 열린 공통 대화에 전달되는지 확인합니다.')
        owner.turn(parent, f'Send followup_task to the SAME external_worker child ({child}), do not spawn another. '
            'Ask it to append exactly |B to result.txt using a shell tool, verify exact bytes with a shell assertion, '
            'and wait for completion. Do no file work yourself.', timeout=480)
        completions = [m for m in owner.notifications if m['method'] == 'turn/completed' and m['params'].get('threadId') == parent]
        turn_id = completions[-1]['params']['turn']['id']
        reader.wait_for(lambda message: message['method'] == 'turn/completed'
            and message['params'].get('threadId') == projection and message['params']['turn']['id'] == turn_id)
        source_replies = {m['params']['item']['id']: m['params']['item']['text'] for m in owner.notifications
            if m['method'] == 'item/completed' and m['params'].get('threadId') == parent
            and m['params'].get('turnId') == turn_id and m['params']['item'].get('type') == 'agentMessage'}
        reader_replies = {m['params']['item']['id']: m['params']['item']['text'] for m in reader.notifications
            if m['method'] == 'item/completed' and m['params'].get('threadId') == projection
            and m['params'].get('turnId') == turn_id and m['params']['item'].get('type') == 'agentMessage'}
        if not source_replies or source_replies != reader_replies:
            raise RuntimeError('Visible replies differ from canonical assistant replies.')
        report['checks']['open_record_receives_same_turn_and_reply_items'] = True
        report['matching_reply_items'] = len(source_replies)
        health = AdminClient(ROOT, endpoint_id(profiles[1]['id'], 'remote-dev'), profiles[1]['generation']).request('manager/maintenance/status', {})
        if any(type(health.get(field)) is not int or health[field] != 0 for field in ACTIVITY_FIELDS):
            raise RuntimeError('Foreign history was counted as work in the reader profile.')
        report['reader_activity'] = {field: health[field] for field in ACTIVITY_FIELDS}
        if reader.request('thread/loaded/list', {})['data']:
            raise RuntimeError('A passive reader has execution actors.')
        report['checks']['reader_has_no_execution_activity_or_loaded_actors'] = True
        final = evidence(remote, bindings[0], parent, workspace)
        rows = [row for row in final['threads'] if row['thread_id'] == child]
        if (len(rows) != 1 or rows[0]['provider'] != role['runtime_provider_id'] or len(rows[0]['turns']) != 2
                or any(t.get('model') != 'deepseek-flash' or t.get('effort') != 'max'
                       or not t.get('completed') or t['successful_commands'] < 1 for t in rows[0]['turns'])
                or final['result_sha256'] != hashlib.sha256((nonce + '|A|B').encode()).hexdigest()):
            raise RuntimeError('Flash max follow-up tool/file evidence mismatch.')
        report['execution_evidence'] = final
        parents = [row for row in final['threads'] if row['thread_id'] == parent]
        if len(parents) != 1 or any(parents[0][key] for key in ('other_tool_calls', 'command_items', 'file_change_items', 'unknown_tool_items')):
            raise RuntimeError('Parent performed file work instead of the external child.')
        report['checks']['same_flash_max_child_performs_both_file_turns'] = True
        viewer_files = evidence(remote, bindings[1], parent, workspace)
        if viewer_files['session_hashes'] or viewer_files['auth_file_present'] or final['auth_file_present']:
            raise RuntimeError('Reader copied records or a remote login file was created.')
        if reader.methods['thread/list'] != 1 or source_catalog_hash(remote, bindings[0]) != initial_catalog_hash:
            raise RuntimeError('The test republished/relisted records instead of observing them.')
        report['checks'].update(no_source_republication_or_relisting=True, remote_auth_files_absent=True,
                                reader_does_not_copy_history=True, no_account_handoff=True, parent_did_no_file_work=True)
        report['status'] = 'PASS'
    except Exception as error:
        report.update(status='FAIL', error=progress.clean(str(error))[:3500])
        progress.emit('공통 대화 시험 실패: ' + report['error'])
    finally:
        report['cleanup'] = []
        for client in reversed(clients):
            try:
                cleanup = client.close()
                report['cleanup'].append(cleanup)
                if not cleanup.get('observed_test_turns_finished') or cleanup.get('interrupt_unconfirmed'):
                    report.update(status='FAIL', cleanup_error='Test execution completion could not be confirmed.')
            except Exception as error:
                report.update(status='FAIL', cleanup_error=progress.clean(str(error))[:1000])
        report['remote_daemon_exit_verified'] = False
        report['finished_at'] = time.strftime('%Y-%m-%dT%H:%M:%S%z')
        progress.write_report(report)
        progress.close()
    print(json.dumps({'status': report['status'], 'report': str(output / 'report.json'), 'error': report.get('error')}, ensure_ascii=False))
    return 0 if report['status'] == 'PASS' else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--artifact-root', type=Path, required=True)
    args = parser.parse_args()
    if args.execute:
        raise SystemExit(run(args.artifact_root.resolve(strict=True)))
    print('Plan: fresh remote-dev profiles; 04 Astra + Flash max file work; 02 native common-list/title/reply observation. No handoff or GUI.')
