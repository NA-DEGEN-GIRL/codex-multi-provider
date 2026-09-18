"""Actual headless Windows account -> native SSH shim -> Linux model test.

Uses only an already prepared, dedicated test binding. Never drives GUI, changes
stock CLI, copies auth files, or chooses a different account on failure.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path, PurePosixPath
import queue
import shlex
import subprocess
import sys
import threading
import time
from uuid import uuid4

from manager_core.accounts import Accounts
from manager_core.providers import ProviderRegistry
from manager_core.proxy_auth import account_fingerprint, read_existing_tokens
from manager_core.remote import RemoteManager
from manager_core.store import Store
from manager_core.ssh_shim import native_bodies, native_command, prepare_environment, validate_binding
from progress import Progress
from desktop_launch import find_app
from remote_helpers.ws_client import WebSocketPipe
from test_manager_live import ManagedClient

ROOT = Path(__file__).resolve().parents[1]


class SshManagedClient(ManagedClient):
    def __init__(self, executable, arguments, environment, marker, progress, *, notification_opt_outs=None):
        self.progress = progress
        self.process = subprocess.Popen([str(executable), *arguments], cwd=ROOT,
            env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.messages = queue.Queue()
        self.stderr, self.events, self.responses = [], [], {}
        self.counter = 0
        self.active, self.turn_efforts = {}, {}
        self.write_lock = threading.RLock()
        self.ws = WebSocketPipe(self.process.stdout, self.process.stdin)
        threading.Thread(target=self.read_stderr, daemon=True).start()
        try:
            deadline, prefix = time.monotonic() + 40, bytearray()
            for _ in range(1024 * 1024):
                prefix.extend(self.ws._exact(1, deadline))
                if prefix.endswith(marker):
                    break
                if len(prefix) > len(marker):
                    del prefix[:-len(marker)]
            else:
                raise RuntimeError('Native SSH synchronization marker missing.')
            self.ws.handshake(timeout=40)
            threading.Thread(target=self.read_stdout, daemon=True).start()
            self.initialize_result = self.request('initialize', {
                'clientInfo': {'name': 'codex_control_center_ssh_headless', 'title': 'Managed SSH test', 'version': '0.1.0'},
                'capabilities': {'experimentalApi': True,
                                 'optOutNotificationMethods': notification_opt_outs or []}}, timeout=45)
            self.send({'method': 'initialized'})
        except Exception:
            self.close()
            for detail in self.stderr[-8:]:
                progress.emit('SSH 연결 진단: ' + progress.clean(detail).strip()[:1000])
            raise

    def read_stdout(self):
        try:
            while True:
                self.messages.put(self.ws.receive_json(timeout=900))
        except Exception as error:
            # Errors are framing categories only. Never retain peer frame bytes.
            self.stderr.append('WebSocket reader: ' + type(error).__name__ + '\n')
        finally:
            self.messages.put(None)

    def read_stderr(self):
        for raw in self.process.stderr:
            self.stderr.append(self.progress.clean(raw.decode('utf-8', 'replace'))[:3000])
            if len(self.stderr) > 100:
                del self.stderr[:30]

    def send(self, value):
        with self.write_lock:
            self.ws.send_json(value)

    def close(self):
        interrupted, unconfirmed = [], []
        if self.process.poll() is None:
            for thread, turn in list(self.active.items()):
                if thread and turn:
                    try:
                        self.request('turn/interrupt', {'threadId': thread, 'turnId': turn}, timeout=10)
                        interrupted.append(thread)
                    except Exception:
                        unconfirmed.append(thread)
            deadline = time.monotonic() + 5
            while self.active and time.monotonic() < deadline:
                try:
                    self.next_message(max(0.1, deadline - time.monotonic()))
                except Exception:
                    break
            try:
                with self.write_lock:
                    self.ws.send_frame(8, b'\x03\xe8')
            except (OSError, ValueError):
                pass
            try:
                self.process.stdin.close()
            except OSError:
                pass
            try:
                self.process.wait(timeout=12)
            except subprocess.TimeoutExpired:
                # Native bootstrap's own job contains only this SSH transport tree.
                self.process.terminate()
                self.process.wait(timeout=10)
        return {'observed_test_turns_finished': not self.active,
                'interrupt_requested': interrupted, 'interrupt_unconfirmed': unconfirmed,
                'unfinished_observed_thread_ids': list(self.active),
                'local_transport_exit_code': self.process.poll(),
                'remote_listener_stopped': False}


def remote_python(remote, binding, source, args=()):
    command = shlex.join([binding['remote_python'], '-', *map(str, args)])
    result = remote._run(binding['alias'], command, input=source.encode('utf-8'), timeout=40)
    if result.returncode:
        raise RuntimeError('Scoped remote test helper failed (exit ' + str(result.returncode) + ').')
    try:
        return json.loads(result.stdout)
    except (ValueError, UnicodeError):
        raise RuntimeError('Scoped remote test helper returned invalid JSON.') from None


def readable_report(output, report):
    labels = {
        'native_ssh_websocket_initialize': '배포된 Windows SSH 실행기에서 원격 런타임 초기화',
        'bound_account_type': '선택 계정의 로그인 유형',
        'expected_account_pinned': '선택한 Windows 계정을 연결 전체에 고정',
        'remote_deepseek_file_result': 'DeepSeek 자식이 원격 파일 읽기·쓰기·셸 검증',
        'remote_same_child_followup_file': '같은 DeepSeek 자식에 후속 작업 전달',
        'remote_auth_file_absent': '원격 테스트 HOME에 로그인 파일을 저장하지 않음',
        'deepseek_flash_always_max': 'DeepSeek Flash의 저장된 모든 실행이 max',
        'native_gpt_sol_recorded': '별도 GPT Sol 자식의 OpenAI 실행 기록',
        'same_external_child_turns': '같은 외부 자식 ID로 두 번 이상 작업',
        'deepseek_child_tool_execution_each_turn': '각 DeepSeek 실행에서 실제 셸 도구가 종료 코드 0으로 완료',
        'parent_did_not_execute_file_tools': '부모가 자식 대신 파일 도구를 실행하지 않음',
        'observed_test_turns_finished': '관찰된 테스트 부모·자식 실행이 모두 종료',
        'remote_workspace_sandbox_exec': '원격 workspace-write 샌드박스에서 실제 명령 실행',
    }
    lines = ['# Windows → SSH 실제 모델 검증', '',
        f'결과: **{report["status"]}** · Windows 계정 별칭 **{report["account_alias"]}** · SSH **{report["host_alias"]}**', '',
        f'실행: {report["started_at"]} → {report.get("finished_at", "진행 중")}', '',
        '배포된 Windows SSH 실행기에서 원본 앱과 같은 명령 형식·WebSocket 연결로 격리된 Linux 패치 런타임에 연결했습니다. '
        '지정한 Windows 계정의 access token은 메모리에서만 전달했고 refresh token과 로그인 파일은 복사하지 않았습니다.', '',
        '| 검사 | 결과 |', '|---|---|']
    for key, value in report['checks'].items():
        lines.append(f'| {labels.get(key, key)} | {"통과" if value is True else "실패" if value is False else value} |')
    if report.get('routes'):
        lines += ['', '| 역할 | 실제 모델 | Provider | 실행 수 | Reasoning |', '|---|---|---|---|---|']
        for route in report['routes']:
            role = '부모' if route['id'] == report['parent']['id'] else 'DeepSeek 자식' if 'deepseek-flash' in route['models'] else 'GPT 자식'
            provider = 'OpenAI' if route['provider'] == 'openai' else '등록된 외부 공급자'
            lines.append(f'| {role} | {", ".join(route["models"])} | {provider} | {route["turn_count"]} | {", ".join(route["reasoning_efforts"])} |')
        lines += ['', '모델·provider·reasoning은 자식의 자기소개가 아니라 원격 테스트 HOME의 저장된 session_meta와 turn_context에서 확인했습니다.', '']
    if report.get('error'):
        lines += ['', '실패: ' + report['error'], '']
    lines += ['', '이 검증은 실제 SSH 연결과 모델 도구 작업을 포함합니다. 원본 GUI 화면의 프로젝트 클릭·대화 선택·창 삽입은 조작하지 않았으므로 그 동작은 별도 확인이 필요합니다.', '',
              '[상세 기록](report.json)', '']
    (output / 'report.md').write_text('\n'.join(lines), encoding='utf-8')


def main(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binding', type=Path, default=ROOT / 'work/remote-dev-smoke-binding.json')
    parser.add_argument('--account', default='02')
    parser.add_argument('--windows-profile-id', help='Use this directly logged-in Windows profile; never fall back to an imported account.')
    parser.add_argument('--scenario', choices=['smoke', 'mixed'], default='mixed')
    parser.add_argument('--admin-probe', action='store_true', help='Verify the existing SSH transport admin pipe without creating model work.')
    parser.add_argument('--managed-idle-probe', action='store_true', help='Create an empty managed thread and verify strict writer release; requires --admin-probe.')
    args = parser.parse_args(arguments)
    if args.managed_idle_probe and not args.admin_probe:
        raise ValueError('--managed-idle-probe requires --admin-probe')
    binding = json.loads(args.binding.read_text(encoding='utf-8-sig'))
    validate_binding(binding, binding['profile_id'])
    approval = ROOT / 'work/approved-ssh-test-profiles.json'
    approved_profiles = json.loads(approval.read_text(encoding='utf-8')) if approval.is_file() else []
    if not isinstance(approved_profiles, list) or binding['profile_id'] not in approved_profiles:
        raise RuntimeError('This live driver requires a dedicated test profile enrolled in work/approved-ssh-test-profiles.json.')
    expected_remote_home = str(PurePosixPath(binding['remote_launcher']).parent / 'codex')
    if binding.get('remote_profile_home') != expected_remote_home:
        raise RuntimeError('Remote evidence HOME does not match the dedicated managed launcher.')
    run_id = 'manager-ssh-live-' + time.strftime('%Y%m%d-%H%M%S') + '-' + uuid4().hex[:6]
    output = ROOT / 'artifacts/results' / run_id
    progress = Progress(output)
    report = dict(run_id=run_id, scenario=args.scenario, account_alias=args.account,
        host_alias=binding['alias'], profile_id=binding['profile_id'], revision=binding['revision'],
        started_at=time.strftime('%Y-%m-%dT%H:%M:%S%z'), status='RUNNING', checks={})
    client, remote, workspace = None, None, None
    try:
        if args.windows_profile_id:
            profile = Store(ROOT).profile(args.windows_profile_id)
            if profile.get('auth_mode') != 'native' or profile.get('login_state') != 'signed_in':
                raise RuntimeError('The selected Windows profile has not completed direct login verification.')
            account = {'home': profile['home'], 'alias': profile['alias']}
            report.update(account_alias=profile['alias'], windows_profile_id=profile['id'], auth_source='windows_native')
        else:
            accounts = [a for a in Accounts(ROOT).list() if a['alias'] == args.account]
            if len(accounts) != 1:
                raise RuntimeError('Requested registered account alias is missing or ambiguous.')
            account = accounts[0]
        tokens = read_existing_tokens(account['home'])
        expected_fingerprint = account_fingerprint(tokens.account_id)
        if args.windows_profile_id and expected_fingerprint != profile.get('account_fingerprint'):
            raise RuntimeError('The Windows login identity changed after verification.')
        progress.add_secret(tokens.access_token)
        progress.add_secret(tokens.account_id)
        del tokens
        registry = ProviderRegistry(ROOT)
        for value in registry.environment(binding['model_ids']).values():
            progress.add_secret(value)
        generated = registry.render_for_host(binding['remote_profile_home'], True, binding['model_ids'])
        roles = [b for b in generated['bindings'] if b['wire_model_id'] == 'deepseek-flash']
        if len(roles) != 1 or roles[0]['reasoning_effort'] != 'max':
            raise RuntimeError('Expected exactly one registered DeepSeek Flash max binding.')
        role = roles[0]
        report['binding'] = {k: role[k] for k in ('model_id', 'role_id', 'wire_model_id', 'runtime_provider_id', 'reasoning_effort')}
        manifest = json.loads((ROOT / 'artifacts/manager/current.json').read_text(encoding='utf-8-sig'))
        executable = Path(manifest['ssh_proxy']).resolve(strict=True)
        if not executable.is_relative_to(ROOT / 'artifacts/manager'):
            raise RuntimeError('Published SSH bootstrap is outside the manager releases.')
        environment = {k: v for k, v in os.environ.items() if not k.upper().startswith('CODEX_') and k != 'ELECTRON_RUN_AS_NODE'}
        environment.update(CODEX_MANAGER_ROOT=str(ROOT), CODEX_MANAGER_PYTHON=sys.executable,
            CODEX_MANAGER_AUTH_SOURCE=account['home'], CODEX_MANAGER_EXPECTED_ACCOUNT_FINGERPRINT=expected_fingerprint)
        if args.windows_profile_id:
            environment['CODEX_MANAGER_SSH_AUTH_SOURCE']=environment.pop('CODEX_MANAGER_AUTH_SOURCE')
        if args.admin_probe:
            environment['CODEX_MANAGER_GENERATION']=str(uuid4())
        from manager_core.ssh_compatibility import fingerprint
        installed_app = find_app()
        environment = prepare_environment(ROOT, binding['profile_id'], [binding], environment,
            ssh_proxy=executable, app_version=installed_app['Version'],
            app_source_sha256=fingerprint(installed_app['executable']))
        marker = os.urandom(8)
        ssh_args = ['-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', binding['alias'],
                    native_command(native_bodies()['native-proxy'], marker)]
        report['bootstrap'] = str(executable)
        progress.write_report(report)
        progress.emit(f'배포된 SSH 실행기와 WebSocket 연결로 Windows {args.account} 계정을 원격 런타임에 연결합니다.')
        remote = RemoteManager(ROOT)
        start = remote._run(binding['alias'], shlex.join([binding['remote_python'], binding['remote_launcher'],
                            binding['revision'], 'native-start']), timeout=30)
        if start.returncode:
            raise RuntimeError('The dedicated remote test listener could not start.')
        client = SshManagedClient(executable, ssh_args, environment, marker, progress,
            notification_opt_outs=['process/exited','thread/closed'] if args.admin_probe else None)
        account_result = client.request('account/read', {'refreshToken': False}, timeout=45)
        account_type = (account_result.get('account') or {}).get('type')
        if account_type != 'chatgpt':
            raise RuntimeError('Remote runtime did not report the bound ChatGPT account.')
        report['checks'].update(native_ssh_websocket_initialize=True, bound_account_type=account_type,
                                expected_account_pinned=True)
        progress.write_report(report)
        progress.emit('실제 SSH/WebSocket 초기화와 지정 계정 연결이 성공했습니다.')
        if args.admin_probe:
            from manager_core.runtime_admin import AdminClient
            from manager_core.ssh_runtime_control import endpoint_id
            admin = AdminClient(ROOT, endpoint_id(binding['profile_id'],binding['alias']), environment['CODEX_MANAGER_GENERATION'])
            health = admin.request('manager/maintenance/status',{})
            if not all(health.get(k) is True for k in ('connected','initialized','streamComplete','accountReady')):
                raise RuntimeError('SSH transport admin observation is incomplete.')
            loaded = admin.request('thread/loaded/list',{})
            tx=str(uuid4())
            lease=admin.request('manager/maintenance/acquire',{'transactionId':tx})
            try:
                rejected=False
                try:
                    client.request('turn/start',{'threadId':str(uuid4()),'input':[]},timeout=10)
                except RuntimeError as error:
                    rejected='"code": -32044' in str(error)
                if not rejected:
                    raise RuntimeError('SSH transport did not fence new frontend work.')
            finally:
                admin.request('manager/maintenance/release',{'transactionId':tx,'leaseToken':lease['leaseToken']})
            if admin.request('manager/maintenance/status',{})['held'] is not False:
                raise RuntimeError('SSH transport maintenance was not released.')
            report['checks'].update(ssh_admin_existing_connection=True,frontend_opt_outs_preserved_with_complete_observation=True,
                ssh_transport_freeze_blocks_new_work=True,ssh_transport_freeze_released=True)
            report['ssh_admin']={'loaded_thread_count':len(loaded['data']),'remote_daemon_exit_verified':False,
                                 'scope':'one_existing_windows_ssh_transport'}
            progress.emit('기존 SSH 연결의 관리 통신·새 작업 차단·차단 해제 검증을 통과했습니다.')
            if args.managed_idle_probe:
                started=client.request('thread/start',{'model':'gpt-6-astra','cwd':str(PurePosixPath(binding['remote_launcher']).parent),
                    'approvalPolicy':'never','sandbox':'read-only'})
                thread_id=started['thread']['id']
                idle=admin.request('thread/managedIdleStatus',{'threadId':thread_id})
                if idle.get('idle') is not True or idle.get('sourceStoreId')!='manager:'+binding['profile_id']:
                    raise RuntimeError('Remote canonical thread activity could not be verified.')
                tx=str(uuid4())
                lease=admin.request('manager/maintenance/acquire',{'transactionId':tx})
                try:
                    proof=admin.request('thread/managedCloseIdle',{'threadId':thread_id},timeout=60)
                    if proof.get('writerReleaseVerified') is not True or thread_id in admin.request('thread/loaded/list',{})['data']:
                        raise RuntimeError('Remote canonical writer release is unverified.')
                finally:
                    admin.request('manager/maintenance/release',{'transactionId':tx,'leaseToken':lease['leaseToken']})
                report['checks'].update(remote_canonical_source_bound=True,remote_managed_idle_verified=True,
                                        remote_empty_thread_writer_release=True)
                report['managed_idle_probe']={'thread_id':thread_id,'source_store_id':idle['sourceStoreId'],
                                              'model_turns_started':0,'writer_release_verified':True}
                progress.emit('원격 원본 저장소 연결·유휴 상태·빈 대화의 기록 쓰기 종료 검증을 통과했습니다.')
        remote = RemoteManager(ROOT)
        managed_root = PurePosixPath(binding['remote_launcher']).parents[2]
        workspace = str(managed_root / 'live-workspaces' / run_id)
        report['workspace'] = workspace
        if args.scenario != 'smoke':
            nonce = 'nonce-' + uuid4().hex
            remote_python(remote, binding, 'import pathlib,subprocess,sys,json\np=pathlib.Path(sys.argv[1]); p.mkdir(parents=True,exist_ok=False)\n(p/"input.txt").write_text(sys.argv[2],encoding="utf-8")\nsubprocess.run(["git","init","-q",str(p)],check=True,stdout=subprocess.DEVNULL)\nprint(json.dumps({"created":True}))', [workspace, nonce])
            sandbox_probe = client.request('command/exec', {
                'command': [binding['remote_python'], '-c', 'print("sandbox_ok")'],
                'cwd': workspace, 'timeoutMs': 20000, 'outputBytesCap': 4000,
                'sandboxPolicy': {'type': 'workspaceWrite', 'writableRoots': [workspace],
                                  'networkAccess': False, 'excludeTmpdirEnvVar': False,
                                  'excludeSlashTmp': False}}, timeout=30)
            if sandbox_probe.get('exitCode') != 0 or sandbox_probe.get('stdout', '').strip() != 'sandbox_ok':
                report['sandbox_probe'] = progress.clean(sandbox_probe)
                raise RuntimeError('Remote workspace sandbox could not execute the prerequisite probe.')
            report['checks']['remote_workspace_sandbox_exec'] = True
            progress.write_report(report)
            instructions = (f'The user explicitly authorizes native V2 delegation. Keep all file work inside {workspace}. '
                'Never read credentials or configuration outside the assigned workspace. Do not delegate beyond one level. '
                f'For DeepSeek use external_agents.spawn_agent with agent_type {role["role_id"]} and fork_turns none. '
                'For GPT use collaboration.spawn_agent with model gpt-5.6-sol and fork_turns none. '
                'Report actual failures; never substitute another provider for a failed external child.')
            started = client.request('thread/start', {'model': 'gpt-6-astra', 'cwd': workspace,
                'approvalPolicy': 'never', 'sandbox': 'workspace-write', 'developerInstructions': instructions})
            thread_id = started['thread']['id']
            report['parent'] = dict(id=thread_id, model=started['model'], provider=started['modelProvider'])
            if started['model'] != 'gpt-6-astra' or started['modelProvider'] != 'openai':
                raise RuntimeError('Parent model substitution detected.')
            progress.register_thread(thread_id, started['model'], started['modelProvider'], 'SSH Astra 부모')
            prompt = (f'This is an authorized native V2 mixed-provider SSH test. Do not read or edit input.txt yourself. '
                f'Spawn external_worker using external_agents.spawn_agent, agent_type {role["role_id"]}, fork_turns none. '
                'Ask it to read input.txt with a shell tool, write result.txt containing exactly the input followed by |deepseek, '
                'then verify result.txt using a shell assertion. Also spawn a separate gpt_worker via collaboration.spawn_agent '
                'with model gpt-5.6-sol, fork_turns none, to compute 37*19 independently. Wait for both, keep their IDs, '
                'and report actual models and results. Do not replace a failed child or do its work yourself.')
            messages = client.turn(thread_id, prompt, timeout=540)
            report['messages'] = messages
            progress.write_report(report)
            result = remote_python(remote, binding, 'import pathlib,sys,json\np=pathlib.Path(sys.argv[1])/"result.txt"\nprint(json.dumps({"result":p.read_text(encoding="utf-8") if p.is_file() else None}))', [workspace])
            if result['result'] is None:
                raise RuntimeError('Remote DeepSeek did not create result.txt. See the recorded worker result for the actual tool error.')
            if result['result'] != nonce + '|deepseek':
                raise RuntimeError('Remote DeepSeek file result mismatch.')
            report['checks']['remote_deepseek_file_result'] = True
            progress.write_report(report)
            messages += client.turn(thread_id, 'Send a followup_task to the SAME external_worker DeepSeek child, not a new agent. '
                'Ask it to append |followup to result.txt and verify with a shell assertion. Wait for it. Do not edit the file yourself.', timeout=420)
            result = remote_python(remote, binding, 'import pathlib,sys,json\nprint(json.dumps({"result":(pathlib.Path(sys.argv[1])/"result.txt").read_text(encoding="utf-8")}))', [workspace])
            if result['result'] != nonce + '|deepseek|followup':
                raise RuntimeError('Remote same-child follow-up file mismatch.')
            report['checks']['remote_same_child_followup_file'] = True
            report['messages'] = messages
        report['status'] = 'PASS'
    except Exception as error:
        report.update(status='FAIL', error=progress.clean(str(error))[:2400])
        progress.emit('실험 오류: ' + report['error'])
    finally:
        if client is not None:
            try:
                report['cleanup'] = client.close()
                report['checks']['observed_test_turns_finished'] = report['cleanup']['observed_test_turns_finished']
            except Exception as error:
                report.update(status='FAIL', cleanup_error=progress.clean(str(error))[:1000])
        if remote is not None:
            try:
                evidence_source = '''import pathlib,sys,json
home=pathlib.Path(sys.argv[1]); parent=sys.argv[2]; routes=[]
assert home.resolve()==home and home.name=='codex' and home.parent.name==sys.argv[3]
for file in (home/'sessions').rglob('*.jsonl'):
    if not file.resolve().is_relative_to(home): continue
    metadata=None; contexts=[]; commands=[]; file_tools=0
    try:
        for line in file.read_text(encoding='utf-8').splitlines():
            row=json.loads(line)
            if row.get('type')=='session_meta': metadata=row['payload']
            elif row.get('type')=='turn_context': contexts.append(row['payload'])
            elif row.get('type')=='event_msg' and row.get('payload',{}).get('type')=='item_completed':
                event=row['payload']; item=event.get('item',{})
                if item.get('type')=='CommandExecution':
                    commands.append({'turn_id':event.get('turn_id'),'exit_code':item.get('exit_code'),
                        'cwd_matches':item.get('cwd') in (sys.argv[4], pathlib.Path(sys.argv[4]).as_uri())})
                if item.get('type') in ('CommandExecution','FileChange'): file_tools+=1
    except (OSError,ValueError): continue
    if not metadata: continue
    source=metadata.get('source',{})
    if metadata.get('id')!=parent and parent not in json.dumps(source): continue
    routes.append({'id':metadata.get('id'),'provider':metadata.get('model_provider'),
        'source':source,'models':list(dict.fromkeys(c.get('model') for c in contexts)),
        'turn_count':len(contexts),'turn_ids':list(dict.fromkeys(c.get('turn_id') for c in contexts)),
        'reasoning_efforts':list(dict.fromkeys(c.get('effort') for c in contexts)),
        'command_evidence':commands,'file_tool_count':file_tools})
print(json.dumps({'routes':routes,'auth_file_created':(home/'auth.json').exists()}))
'''
                evidence = remote_python(remote, binding, evidence_source, [binding['remote_profile_home'],
                    report.get('parent', {}).get('id', 'no-parent'), binding['profile_id'], workspace or ''])
                report.update(evidence)
                report['checks']['remote_auth_file_absent'] = not evidence['auth_file_created']
                if report.get('parent'):
                    external = [r for r in evidence['routes'] if r['provider'] == role['runtime_provider_id']]
                    native = [r for r in evidence['routes'] if r['id'] != report['parent']['id'] and r['provider'] == 'openai' and 'gpt-5.6-sol' in r['models']]
                    parents = [r for r in evidence['routes'] if r['id'] == report['parent']['id']]
                    report['checks'].update(deepseek_flash_always_max=bool(external) and all(r['reasoning_efforts'] == ['max'] and r['models'] == ['deepseek-flash'] for r in external),
                        native_gpt_sol_recorded=bool(native), same_external_child_turns=len(external) == 1 and external[0]['turn_count'] >= 2)
                    report['checks']['deepseek_child_tool_execution_each_turn'] = bool(external) and all(
                        r['turn_ids'] and all(any(c['turn_id'] == turn and c['exit_code'] == 0 and c['cwd_matches']
                            for c in r['command_evidence']) for turn in r['turn_ids']) for r in external)
                    report['checks']['parent_did_not_execute_file_tools'] = len(parents) == 1 and parents[0]['file_tool_count'] == 0
                if any(value is False for value in report['checks'].values()):
                    report.update(status='FAIL', error=report.get('error', 'Persisted remote route evidence is incomplete.'))
            except Exception as error:
                report.update(status='FAIL', evidence_error=progress.clean(str(error))[:1000])
        report['finished_at'] = time.strftime('%Y-%m-%dT%H:%M:%S%z')
        progress.write_report(report)
        readable_report(output, report)
        progress.emit('최종 상태: ' + report['status'])
        progress.emit('보고서: ' + str(output / 'report.json'))
        progress.close()
    print(json.dumps({'status': report['status'], 'report': str(output / 'report.json'), 'checks': report['checks'], 'error': report.get('error')}, ensure_ascii=False), flush=True)
    return 0 if report['status'] == 'PASS' else 1


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
