"""Headless, isolated end-to-end test of the shipped native manager bootstrap.

Never opens or closes desktop windows. Uses a fresh unlisted manager profile,
registered account access-token binding, and the generic provider registry.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import time
from uuid import UUID, uuid4

from live_test import Client, collect_routes
from manager_core.accounts import Accounts
from manager_core.providers import ProviderRegistry
from progress import Progress

ROOT = Path(__file__).resolve().parents[1]


def safe_report_leaf(output, name):
    directory = Path(output).resolve(strict=True)
    path = directory / name
    if name not in ('report.json', 'report.json.tmp', 'report.md') or path.is_symlink():
        raise RuntimeError('The test report path is not a regular scoped file.')
    if path.resolve().parent != directory or (path.exists() and not path.is_file()):
        raise RuntimeError('The test report path is outside its result directory.')
    return path


def read_test_bytes(workspace, name):
    directory = Path(workspace).resolve(strict=True)
    path = directory / name
    if name not in ('input.txt', 'result.txt') or path.is_symlink() or path.resolve(strict=True).parent != directory:
        raise RuntimeError('The test file is outside its assigned workspace.')
    if path.stat().st_size > 1024 * 1024:
        raise RuntimeError('The bounded nonce test file is unexpectedly large.')
    return path.read_bytes()


def verify_output_bytes(workspace, expected, report, stage):
    """Record only byte counts and digests, never trim output or accept a BOM."""
    actual = read_test_bytes(workspace, 'result.txt')
    matched = actual == expected
    report.setdefault('output_byte_checks', {})[stage] = {
        'matched': matched, 'actual_bytes': len(actual), 'expected_bytes': len(expected),
        'actual_sha256': hashlib.sha256(actual).hexdigest(),
        'expected_sha256': hashlib.sha256(expected).hexdigest(),
        'validated_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
    }
    if not matched:
        raise RuntimeError('The test output does not match the exact expected bytes: ' + stage)


_AGENT_CONTROL_TOOLS = {
    'spawn_agent', 'wait_agent', 'list_agents', 'send_message', 'followup_task', 'interrupt_agent',
    'collaboration.spawn_agent', 'collaboration.wait_agent', 'collaboration.list_agents',
    'collaboration.send_message', 'collaboration.followup_task', 'collaboration.interrupt_agent',
    'external_agents.spawn_agent', 'external_agents.wait_agent', 'external_agents.list_agents',
    'external_agents.send_message', 'external_agents.followup_task', 'external_agents.interrupt_agent',
}
_NON_FILE_ITEMS = {'AgentMessage', 'UserMessage', 'Reasoning', 'Plan', 'SubAgentActivity',
                   'CollabAgentToolCall', 'ContextCompaction', 'ToolSearch', 'SearchToolCall'}
_NON_TOOL_RESPONSE_ITEMS = {'message', 'reasoning', 'agent_message', 'function_call_output',
                            'custom_tool_call_output', 'compaction'}


def collect_execution_evidence(home, parent_id):
    """Read only this test HOME and retain allowlisted execution metadata.

    Commands, function arguments/results, messages and reasoning never enter the
    returned object. Unknown tool kinds make the parent-only-delegation check
    fail instead of assuming they could not perform file work.
    """
    home = Path(home).resolve(strict=True)
    session_root = home / 'sessions'
    if session_root.is_symlink():
        raise RuntimeError('The test session directory must not be a link.')
    records, errors, unexpected_sessions = {}, 0, 0
    safe_value = re.compile(r'[A-Za-z0-9_./:-]{1,200}\Z')

    def value(item):
        return item if isinstance(item, str) and safe_value.fullmatch(item) else None

    for file in session_root.rglob('*.jsonl'):
        if file.is_symlink() or not file.resolve().is_relative_to(session_root):
            errors += 1
            continue
        record = None
        try:
            with file.open(encoding='utf-8') as source:
                for line in source:
                    row = json.loads(line)
                    payload = row.get('payload', {})
                    if not isinstance(payload, dict):
                        continue
                    kind = row.get('type')
                    if kind == 'session_meta':
                        identity = value(payload.get('id'))
                        origin = payload.get('source')
                        spawn = origin.get('subagent', {}).get('thread_spawn', {}) if isinstance(origin, dict) else {}
                        direct_parent = value(spawn.get('parent_thread_id'))
                        if identity != parent_id and direct_parent != parent_id:
                            unexpected_sessions += 1
                            break
                        if not identity or identity in records:
                            raise ValueError()
                        record = {'thread_id': identity, 'parent_thread_id': direct_parent,
                                  'provider': value(payload.get('model_provider')),
                                  'agent_role': value(spawn.get('agent_role')),
                                  'turns': {}, 'agent_control_calls': 0, 'other_tool_calls': 0,
                                  'command_items': 0, 'file_change_items': 0, 'unknown_tool_items': 0}
                        records[identity] = record
                    if record is None:
                        continue
                    if kind == 'response_item':
                        if payload.get('type') in ('function_call', 'custom_tool_call'):
                            if payload.get('name') in _AGENT_CONTROL_TOOLS:
                                record['agent_control_calls'] += 1
                            else:
                                record['other_tool_calls'] += 1
                        elif payload.get('type') not in _NON_TOOL_RESPONSE_ITEMS:
                            record['other_tool_calls'] += 1
                    if kind == 'turn_context':
                        turn_id = value(payload.get('turn_id'))
                        if not turn_id:
                            raise ValueError()
                        turn = record['turns'].setdefault(turn_id, {'turn_id': turn_id, 'completed': False, 'commands': {}})
                        model, effort = value(payload.get('model')), value(payload.get('effort'))
                        if not model or not effort or ('model' in turn and (turn['model'], turn['effort']) != (model, effort)):
                            raise ValueError()
                        turn.update(model=model, effort=effort)
                    if kind != 'event_msg':
                        continue
                    event = payload.get('type')
                    if event == 'task_complete':
                        turn_id = value(payload.get('turn_id'))
                        if not turn_id:
                            raise ValueError()
                        record['turns'].setdefault(turn_id, {'turn_id': turn_id, 'commands': {}})['completed'] = True
                    if event != 'item_completed':
                        continue
                    item = payload.get('item', {})
                    item_kind = item.get('type')
                    if item_kind == 'CommandExecution':
                        record['command_items'] += 1
                        turn_id, item_id = value(payload.get('turn_id')), value(item.get('id'))
                        if payload.get('thread_id') != record['thread_id'] or not turn_id or not item_id:
                            raise ValueError()
                        turn = record['turns'].setdefault(turn_id, {'turn_id': turn_id, 'completed': False, 'commands': {}})
                        exit_code = item.get('exit_code')
                        turn['commands'][item_id] = {
                            'item_id': item_id,
                            'status': item.get('status') if item.get('status') in ('completed', 'failed', 'in_progress') else 'unknown',
                            'exit_code': exit_code if type(exit_code) is int else None,
                        }
                    elif item_kind == 'FileChange':
                        record['file_change_items'] += 1
                    elif item_kind not in _NON_FILE_ITEMS:
                        record['unknown_tool_items'] += 1
        except (OSError, ValueError, TypeError, AttributeError, RecursionError):
            errors += 1
    for record in records.values():
        for turn in record['turns'].values():
            turn['commands'] = list(turn['commands'].values())
            turn['successful_commands'] = sum(command['status'] == 'completed' and command['exit_code'] == 0
                                               for command in turn['commands'])
        record['turns'] = list(record['turns'].values())
    return {'schema_version': 1, 'source': 'session_meta_turn_context_and_completed_runtime_items',
            'parse_errors': errors, 'unexpected_session_records': unexpected_sessions,
            'threads': list(records.values())}


def apply_execution_checks(home, report):
    evidence = collect_execution_evidence(home, report['parent']['id'])
    report['execution_evidence'] = evidence
    records = evidence['threads']
    parent = [record for record in records if record['thread_id'] == report['parent']['id']]
    children = [record for record in records if record['parent_thread_id'] == report['parent']['id']]
    binding = report['binding']
    external = [record for record in children if record['provider'] == binding['runtime_provider_id']]
    native = [record for record in children if record['provider'] == 'openai' and record['turns']
              and all(turn.get('model') == 'gpt-5.6-sol' for turn in record['turns'])]
    needed_turns = 3 if report['scenario'] == 'cold-resume' else 2
    parent_complete = len(parent) == 1 and len(parent[0]['turns']) >= needed_turns and all(
        turn.get('completed') and turn.get('model') == 'gpt-6-astra' for turn in parent[0]['turns'])
    parent_delegation = parent_complete and parent[0]['provider'] == 'openai' and parent[0]['agent_control_calls'] >= needed_turns + 1
    exact_external = len(external) == 1 and bool(external[0]['turns']) and all(
        turn.get('model') == binding['wire_model_id'] == 'deepseek-flash' for turn in external[0]['turns'])
    external_turns = external[0]['turns'] if len(external) == 1 else []
    checks = {
        'execution_evidence_complete': evidence['parse_errors'] == 0 and evidence['unexpected_session_records'] == 0 and parent_complete,
        'parent_completed_delegation_turns': parent_delegation,
        'external_exact_model_provider': exact_external,
        'deepseek_flash_always_max': exact_external and all(turn.get('effort') == 'max' for turn in external_turns),
        'native_gpt_sol_recorded': len(native) == 1 and all(turn.get('completed') for record in native for turn in record['turns']),
        'only_expected_children': len(children) == 2 and len(external) == 1 and len(native) == 1
            and evidence['unexpected_session_records'] == 0 and all(record['agent_control_calls'] == 0 for record in children),
        'gpt_child_no_file_tools': len(native) == 1 and all(native[0][key] == 0 for key in
            ('other_tool_calls', 'command_items', 'file_change_items', 'unknown_tool_items')),
        'same_external_child_turns': exact_external and len(external_turns) >= needed_turns,
        'external_completed_commands_each_turn': exact_external and len(external_turns) >= needed_turns
            and all(turn.get('completed') and turn['successful_commands'] >= 1 for turn in external_turns),
        'parent_no_file_tools': parent_delegation and all(parent[0][key] == 0 for key in
            ('other_tool_calls', 'command_items', 'file_change_items', 'unknown_tool_items')),
    }
    report['checks'].update(checks)
    if not all(checks.values()):
        report.update(status='FAIL', error=report.get('error', 'Saved model, completed tool, or parent delegation evidence is incomplete.'))


def revalidate_saved_run(output):
    """Enrich an existing test report without launching models, GUI or runtimes."""
    output = Path(output).resolve(strict=True)
    results = (ROOT / 'artifacts/results').resolve()
    if output.parent != results or not re.fullmatch(r'manager-live-\d{8}-\d{6}-[0-9a-f]{6}', output.name):
        raise RuntimeError('Only a saved manager live test result can be revalidated.')
    path = safe_report_leaf(output, 'report.json')
    report = json.loads(path.read_text(encoding='utf-8'))
    if report.get('run_id') != output.name or report.get('scenario') not in ('mixed', 'cold-resume'):
        raise RuntimeError('This saved run has no mixed-provider model evidence.')
    profile_id = str(UUID(report['profile_id']))
    expected_home = ROOT / 'work/control-center/profiles' / profile_id / 'codex'
    expected_workspace = ROOT / 'work/control-center/live-workspaces' / output.name
    for expected, supplied in ((expected_home, report['home']), (expected_workspace, report['workspace'])):
        if Path(supplied) != expected or expected.resolve(strict=True) != expected:
            raise RuntimeError('Saved test paths do not match their isolated profile and workspace.')
    previous_status = report['status']
    report['evidence_revalidation'] = {
        'validated_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'mode': 'saved_files_only_no_model_calls',
        'previous_status': previous_status, 'original_execution_timestamps_preserved': True,
        'historical_intermediate_file_checks': 'legacy_normalized_text_checks_not_reconstructed',
    }
    apply_execution_checks(expected_home, report)
    expected = read_test_bytes(expected_workspace, 'input.txt') + b'|deepseek|followup'
    if report['scenario'] == 'cold-resume':
        expected += b'|coldresume'
    try:
        verify_output_bytes(expected_workspace, expected, report, 'saved_final_output')
        report['checks']['saved_final_output_exact_bytes'] = True
    except (OSError, RuntimeError):
        report['checks']['saved_final_output_exact_bytes'] = False
        report.update(status='FAIL', error=report.get('error', 'The saved final output differs from the exact expected bytes.'))
    temporary = safe_report_leaf(output, 'report.json.tmp')
    temporary.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
    temporary.replace(path)
    write_readable_report(output, report)
    return report


def write_readable_report(output, report):
    checks = report.get('checks', {})
    labels = {
        'native_bootstrap_initialize': '배포된 Windows 실행기로 실제 패치 런타임 초기화',
        'mixed_file_result': 'Astra가 만든 DeepSeek 자식의 파일 작성·셸 검증',
        'same_child_followup_file': '같은 DeepSeek 자식에 후속 작업 전달',
        'cold_resume_same_child_file': '런타임 종료·새 프로세스 재개 후 같은 자식으로 작업 계속',
        'deepseek_flash_always_max': 'DeepSeek Flash의 저장된 모든 실행이 max',
        'native_gpt_sol_recorded': '별도 GPT Sol 자식의 OpenAI 실행 기록',
        'only_expected_children': '예상한 두 자식만 실행하고 추가 하위 위임 없음',
        'gpt_child_no_file_tools': '계산용 GPT 자식의 파일·셸 작업 없음',
        'same_external_child_turns': '외부 자식을 교체하지 않은 동일 ID 확인',
        'execution_evidence_complete': '해당 테스트 HOME의 실행 기록을 빠짐없이 해석',
        'external_exact_model_provider': '외부 자식의 정확한 DeepSeek 모델·공급자 확인',
        'external_completed_commands_each_turn': 'DeepSeek의 모든 턴에서 완료된 셸 명령과 종료 코드 0 확인',
        'parent_no_file_tools': 'Astra 부모의 직접 파일·셸 작업 없음',
        'parent_completed_delegation_turns': 'Astra 부모의 위임 호출과 모든 실행 턴 완료 확인',
        'saved_final_output_exact_bytes': '보관된 최종 파일을 바이트 단위로 재검증',
    }
    lines = ['# Windows 관리 런타임 — 실제 헤드리스 검증', '',
             f'결과: **{report["status"]}** · 계정 별칭 **{report["account_alias"]}**', '',
             f'실행: {report["started_at"]} → {report.get("finished_at", "진행 중")}', '',
             '새 테스트 프로필과 새 작업 폴더에서 배포된 C# 실행기 → Python 연결 계층 → 실제 Rust 패치 런타임을 실행했습니다. '
             '기존 Codex 창을 조작하거나 닫지 않았습니다.', '',
             '| 확인 항목 | 결과 |', '|---|---|']
    for key, label in labels.items():
        if key in checks:
            lines.append(f'| {label} | {"통과" if checks[key] else "실패"} |')
    lines += [f'| 지정 계정 연결 | {checks.get("bound_account_type", "미확인")} |',
              f'| 테스트 HOME에 로그인 파일 저장 | {"발생" if report.get("auth_file_created") else "없음"} |', '']
    if report.get('routes'):
        lines += ['## 실제 기록에 남은 모델', '',
                  '아래 모델·provider·reasoning 값은 모델의 자기소개가 아니라 저장된 `session_meta`와 `turn_context`에서 확인했습니다.', '',
                  '| 역할 | 모델 | Provider | 실행 횟수 | Reasoning |', '|---|---|---|---|---|']
        for route in report['routes']:
            parent = route['id'] == report['parent']['id']
            role = '부모' if parent else 'DeepSeek 자식' if 'deepseek-flash' in route['models'] else 'GPT 자식'
            provider = 'OpenAI' if route['provider'] == 'openai' else '등록된 외부 공급자'
            lines.append(f'| {role} | {", ".join(route["models"])} | {provider} | {route["turn_count"]} | {", ".join(route["reasoning_efforts"])} |')
        lines += ['', '파일 결과와 저장된 자식 ID를 별도로 검사했습니다. 콜드 재개 시나리오는 테스트 런타임을 종료한 뒤 '
                  '새 런타임에서 같은 부모와 자식에 작업을 전달한 실행입니다.', '']
    if report.get('execution_evidence'):
        lines += ['## 자식의 실제 도구 실행', '',
                  '명령문·출력·대화 내용을 복사하지 않고, 저장된 CommandExecution의 완료 상태와 종료 코드만 확인했습니다.', '',
                  '| 역할 | 턴 | 모델 | Reasoning | 종료 코드 0인 완료 명령 | 턴 완료 |', '|---|---|---|---|---|---|']
        for record in report['execution_evidence']['threads']:
            role = '부모' if record['thread_id'] == report['parent']['id'] else (
                'DeepSeek 자식' if record['provider'] == report['binding']['runtime_provider_id'] else 'GPT 자식')
            for index, turn in enumerate(record['turns'], 1):
                lines.append(f'| {role} | {index} | {turn.get("model", "미확인")} | {turn.get("effort", "미확인")} | '
                             f'{turn["successful_commands"]} | {"확인" if turn.get("completed") else "미확인"} |')
        lines += ['', '이 검사는 도구의 성공 종료와 파일 바이트를 함께 확인합니다. 각 명령문의 의미를 재실행하거나 '
                  '모델의 자기소개를 실행 증거로 사용하지 않습니다.', '']
    if report.get('evidence_revalidation'):
        validation = report['evidence_revalidation']
        lines += ['## 저장 기록 재검증', '', f'재검증: {validation["validated_at"]}', '',
                  '원래 실행 시각을 유지하고 기존 테스트 HOME과 최종 파일만 읽었습니다. 모델·GUI·런타임은 다시 실행하지 않았습니다. '
                  '과거 중간 단계는 당시 공백을 정리한 텍스트 비교 결과를 유지하며, 해당 시점의 정확한 바이트를 재구성한 것은 아닙니다.', '']
    if report.get('output_byte_checks'):
        lines += ['| 파일 바이트 검사 | 실제 / 기대 크기 | 정확한 일치 |', '|---|---|---|']
        for stage, result in report['output_byte_checks'].items():
            lines.append(f'| {stage} | {result["actual_bytes"]} / {result["expected_bytes"]} | {"통과" if result["matched"] else "실패"} |')
        lines.append('')
    if report.get('error'):
        lines += ['## 실패 내용', '', report['error'], '']
    lines += ['## 확인 범위', '',
              '이 결과는 실제 모델 API와 네이티브 실행기를 거친 로컬 backend 검증입니다. 원본 GUI의 화면 삽입·대화 선택, '
              'SSH 연결, 공식 앱 업데이트 실행은 이 테스트의 범위에 포함되지 않습니다. 자격 증명은 복사하지 않고 '
              '등록 계정의 유효한 access token만 메모리에서 사용했습니다.', '',
              f'상세 기록: [report.json]({(Path(output) / "report.json").as_posix()})', '',
              f'실행 스크립트: [test_manager_live.py]({(ROOT / "scripts/test_manager_live.py").as_posix()})', '']
    safe_report_leaf(output, 'report.md').write_text('\n'.join(lines), encoding='utf-8')


class ManagedClient(Client):
    def __init__(self, binary, environment, workspace, progress):
        self.progress = progress
        self.process = subprocess.Popen([str(binary), '-c', 'features.code_mode_host=true',
                                         'app-server', '--listen', 'stdio://'],
            cwd=workspace, env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace',
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.messages = queue.Queue()
        self.stderr, self.events, self.responses = [], [], {}
        self.counter = 0
        self.active, self.turn_efforts = {}, {}
        threading.Thread(target=self.read_stdout, daemon=True).start()
        threading.Thread(target=self.read_stderr, daemon=True).start()
        try:
            self.initialize_result = self.request('initialize', {
                'clientInfo': {'name': 'codex_control_center_headless', 'title': 'Managed runtime test', 'version': '0.1.0'},
                'capabilities': {'experimentalApi': True}}, timeout=45)
            self.send({'method': 'initialized'})
        except Exception:
            self.close()
            raise

    def next_message(self, timeout):
        end = time.monotonic() + timeout
        while True:
            try:
                message = self.messages.get(timeout=max(0.01, min(0.35, end - time.monotonic())))
                break
            except queue.Empty:
                self.progress.tick()
                if time.monotonic() >= end:
                    raise TimeoutError('Timed out waiting for managed app-server event')
        if message is None:
            raise RuntimeError('Managed app-server exited: ' + self.progress.clean(''.join(self.stderr[-5:]))[:1000])
        if 'id' in message and 'method' not in message:
            self.responses[message['id']] = message
        elif 'id' in message:
            method = message.get('method', '')
            # The manager must consume account refresh itself. Never fall back to
            # another Codex HOME or another account from this test driver.
            if 'requestApproval' in method:
                self.send({'id': message['id'], 'result': {'decision': 'decline'}})
            else:
                self.send({'id': message['id'], 'error': {'code': -32601, 'message': 'Unsupported unattended test request'}})
        else:
            safe = self.progress.consume(message)
            if safe is not None:
                self.events.append(safe)
            params = message.get('params', {})
            if message.get('method') == 'turn/started':
                self.active[params.get('threadId')] = params.get('turn', {}).get('id')
            elif message.get('method') == 'turn/completed':
                self.active.pop(params.get('threadId'), None)
        return message


def main(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scenario', choices=['smoke', 'mixed', 'cold-resume'], default='mixed')
    parser.add_argument('--account', default='02')
    parser.add_argument('--revalidate-report', type=Path, help='Revalidate a saved result directory without running models or GUI')
    args = parser.parse_args(arguments)
    if args.revalidate_report is not None:
        report = revalidate_saved_run(args.revalidate_report)
        print(json.dumps({'status': report['status'], 'checks': report['checks'],
                          'evidence_revalidation': report['evidence_revalidation']}, ensure_ascii=False), flush=True)
        return 0 if report['status'] == 'PASS' else 1
    run_id = 'manager-live-' + time.strftime('%Y%m%d-%H%M%S') + '-' + uuid4().hex[:6]
    profile_id, generation = str(uuid4()), str(uuid4())
    home = ROOT / 'work/control-center/profiles' / profile_id / 'codex'
    workspace = ROOT / 'work/control-center/live-workspaces' / run_id
    output = ROOT / 'artifacts/results' / run_id
    observer = ROOT / 'work/control-center/instances' / profile_id / 'runtime-state.json'
    report = {'run_id': run_id, 'scenario': args.scenario, 'account_alias': args.account,
              'profile_id': profile_id, 'workspace': str(workspace), 'home': str(home),
              'status': 'RUNNING', 'checks': {}, 'started_at': time.strftime('%Y-%m-%dT%H:%M:%S%z')}
    progress = Progress(output, home)
    client = None
    try:
        accounts = [a for a in Accounts(ROOT).list() if a['alias'] == args.account]
        if len(accounts) != 1:
            raise RuntimeError('Requested registered account alias is missing or ambiguous.')
        account = accounts[0]
        registry = ProviderRegistry(ROOT)
        matches = [m for m in registry.list()['models'] if m['wire_model_id'] == 'deepseek-flash']
        if len(matches) != 1:
            raise RuntimeError('Expected exactly one registered DeepSeek Flash binding.')
        model = matches[0]
        home.mkdir(parents=True)
        workspace.mkdir(parents=True)
        subprocess.run(['git', 'init', '-q', str(workspace)], check=True, capture_output=True)
        base_config = ('model = "gpt-6-astra"\nmodel_reasoning_effort = "low"\n'
                       'web_search = "disabled"\napproval_policy = "never"\n'
                       'sandbox_mode = "workspace-write"\ncli_auth_credentials_store = "file"\n'
                       'tool_output_token_limit = 3000\nsuppress_unstable_features_warning = true\n'
                       '[windows]\nsandbox = "unelevated"\n[features]\nenable_request_compression = false\n')
        (home / 'config.toml').write_text(base_config, encoding='utf-8')
        generated = registry.generate(home, True, [model['id']])
        binding = generated['bindings'][0]
        if binding['reasoning_effort'] != 'max':
            raise RuntimeError('DeepSeek Flash policy was not max.')
        bootstrap_manifest = json.loads((ROOT / 'artifacts/manager/current.json').read_text(encoding='utf-8-sig'))
        bootstrap = Path(bootstrap_manifest['runtime_proxy']).resolve()
        if not bootstrap.is_relative_to(ROOT / 'artifacts/manager') or not bootstrap.is_file():
            raise RuntimeError('Published native runtime bootstrap is not available.')
        runtime = ROOT / 'artifacts/runtime/codex.exe'
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith('CODEX_') and k != 'ELECTRON_RUN_AS_NODE'}
        env.update(CODEX_HOME=str(home), CODEX_CLI_PATH=str(bootstrap), CODEX_MANAGER_ROOT=str(ROOT),
                   CODEX_MANAGER_PYTHON=sys.executable, CODEX_MANAGER_REAL_RUNTIME=str(runtime),
                   CODEX_MANAGER_PROFILE_ID=profile_id, CODEX_MANAGER_GENERATION=generation,
                   CODEX_MANAGER_OBSERVER_PATH=str(observer), CODEX_MANAGER_AUTH_SOURCE=account['home'])
        provider_environment = registry.environment([model['id']])
        for value in provider_environment.values():
            progress.add_secret(value)
        env.update(provider_environment)
        with runtime.open('rb') as binary:
            runtime_hash = hashlib.file_digest(binary, 'sha256').hexdigest()
        report.update(bootstrap=str(bootstrap), runtime_sha256=runtime_hash,
                      binding={k: binding[k] for k in ('model_id', 'role_id', 'wire_model_id', 'runtime_provider_id', 'reasoning_effort')})
        progress.write_report(report)
        progress.emit('관리 실행기 → 실제 패치 런타임 → 지정 계정 연결을 확인합니다.')
        client = ManagedClient(bootstrap, env, workspace, progress)
        account_result = client.request('account/read', {'refreshToken': False}, timeout=45)
        account_type = (account_result.get('account') or {}).get('type')
        if account_type != 'chatgpt':
            raise RuntimeError('Managed runtime did not report the bound ChatGPT account.')
        report['checks'].update(native_bootstrap_initialize=True, bound_account_type=account_type)
        progress.write_report(report)
        progress.emit('실제 네이티브 실행기의 초기화와 지정 계정 연결을 확인했습니다.')
        if args.scenario == 'smoke':
            report['status'] = 'PASS'
        else:
            nonce = 'nonce-' + uuid4().hex
            (workspace / 'input.txt').write_text(nonce, encoding='utf-8')
            role = binding['role_id']
            instructions = (f'The user explicitly authorizes native V2 delegation. Keep all file work inside {workspace}. '
                'Never read credentials or configuration outside the assigned workspace. Do not delegate beyond one level. '
                f'For DeepSeek use external_agents.spawn_agent with agent_type {role} and fork_turns none. '
                'For GPT use collaboration.spawn_agent with model gpt-5.6-sol and fork_turns none. '
                'Report actual failures; never substitute another provider for a failed external child.')
            params = {'model': 'gpt-6-astra', 'cwd': str(workspace), 'approvalPolicy': 'never',
                      'sandbox': 'workspace-write', 'developerInstructions': instructions}
            started = client.request('thread/start', params)
            thread_id = started['thread']['id']
            report['parent'] = {'id': thread_id, 'model': started['model'], 'provider': started['modelProvider']}
            assert started['model'] == 'gpt-6-astra', 'Parent model substitution detected.'
            progress.register_thread(thread_id, started['model'], started['modelProvider'], 'Astra 부모')
            prompt = (f'This is an authorized native V2 mixed-provider test. Do not read or edit input.txt yourself. '
                f'Spawn an external_worker using external_agents.spawn_agent, agent_type {role}, fork_turns none. '
                'Ask it to read input.txt with a shell tool, write result.txt containing exactly the input followed by |deepseek, '
                'then verify result.txt using a shell assertion. Also spawn a separate gpt_worker via collaboration.spawn_agent '
                'with model gpt-5.6-sol, fork_turns none, to compute 37*19 independently. Wait for both, keep their IDs, '
                'and report actual models and results. Do not replace a failed child or do its work yourself.')
            messages = client.turn(thread_id, prompt, timeout=480)
            verify_output_bytes(workspace, (nonce + '|deepseek').encode('utf-8'), report, 'mixed_file_result')
            report['checks']['mixed_file_result'] = True
            progress.write_report(report)
            messages += client.turn(thread_id,
                'Send a followup_task to the SAME external_worker DeepSeek child, not a new agent. Ask it to append '
                '|followup to result.txt and verify with a shell assertion. Wait for it. Do not edit the file yourself.', timeout=360)
            verify_output_bytes(workspace, (nonce + '|deepseek|followup').encode('utf-8'), report, 'same_child_followup_file')
            report['checks']['same_child_followup_file'] = True
            report['messages'] = messages
            progress.write_report(report)
            if args.scenario == 'cold-resume':
                progress.emit('완료된 테스트 런타임을 닫고 같은 저장소로 다시 연결합니다.')
                client.close()
                client = None
                env['CODEX_MANAGER_GENERATION'] = str(uuid4())
                client = ManagedClient(bootstrap, env, workspace, progress)
                client.request('account/read', {'refreshToken': False}, timeout=45)
                resumed = client.request('thread/resume', {'threadId': thread_id})
                assert resumed['thread']['id'] == thread_id, 'Cold resume selected a different parent.'
                report['messages'] += client.turn(thread_id,
                    'Continue this authorized test. Send followup_task to the SAME existing external_worker DeepSeek child '
                    'whose ID is in our earlier conversation. Do not spawn a replacement. Ask it to append |coldresume '
                    'to result.txt and verify using a shell assertion. Wait for completion; do not edit it yourself.', timeout=360)
                verify_output_bytes(workspace, (nonce + '|deepseek|followup|coldresume').encode('utf-8'), report, 'cold_resume_same_child_file')
                report['checks']['cold_resume_same_child_file'] = True
            report['status'] = 'PASS'
    except Exception as error:
        report.update(status='FAIL', error=progress.clean(str(error))[:2400])
        progress.emit('실험 오류: ' + report['error'])
    finally:
        if client is not None:
            try:
                client.close()
            except Exception as error:
                report.update(status='FAIL', cleanup_error=progress.clean(str(error))[:1000])
        if report.get('parent'):
            routes = collect_routes(home, report['parent']['id'])
            report['routes'] = routes
            try:
                apply_execution_checks(home, report)
            except (OSError, ValueError, RuntimeError):
                report['checks']['execution_evidence_complete'] = False
                report.update(status='FAIL', error=report.get('error', 'Saved test execution evidence could not be read safely.'))
        if observer.exists():
            report['observer'] = json.loads(observer.read_text(encoding='utf-8'))
        report['auth_file_created'] = (home / 'auth.json').exists()
        if report['auth_file_created']:
            report.update(status='FAIL', error='Unexpected persisted authentication in the fresh managed home.')
        report['finished_at'] = time.strftime('%Y-%m-%dT%H:%M:%S%z')
        progress.write_report(report)
        write_readable_report(output, report)
        progress.emit('최종 상태: ' + report['status'])
        progress.emit('보고서: ' + str(output / 'report.json'))
        progress.close()
    print(json.dumps({'status': report['status'], 'report': str(output / 'report.json'),
                      'checks': report['checks'], 'error': report.get('error')}, ensure_ascii=False), flush=True)
    return 0 if report['status'] == 'PASS' else 1


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
