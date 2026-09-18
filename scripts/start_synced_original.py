"""Launch the original account with shared record refresh and durable append.

No account copies, settings writes, package edits, task navigation or process
termination. Apply an explicitly queued lossless history repair only with its
rollout exclusively closed. An existing original app must first close normally.
"""
import argparse
import ctypes
import json
import os
import re
from pathlib import Path
import subprocess
import sys

from desktop_launch import find_app, child_environment
from manager_core.original_sync_bundle import prepare
from manager_core.runtime_build import resolve
from manager_core.history_recovery import apply_pending


def original_processes():
    command = ("Get-CimInstance Win32_Process -Filter \"Name='ChatGPT.exe'\" | "
        "Where-Object { $_.CommandLine -notmatch '--type=' } | "
        "Select-Object ProcessId,ExecutablePath,CommandLine | ConvertTo-Json -Compress")
    response = subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',command],
        capture_output=True,encoding='utf8',timeout=20,creationflags=subprocess.CREATE_NO_WINDOW)
    response.check_returncode()
    data = json.loads(response.stdout or '[]')
    return data if isinstance(data,list) else [data]


def uses_original_ui(process, root, installed, ui):
    executable = process.get('ExecutablePath')
    if not executable:
        return False
    executable = Path(executable).resolve()
    # Windows can restore a private binary after reboot without its launcher
    # environment/arguments. It then claims the default original UI singleton.
    package = executable.parent.parent
    installed_codex = (executable.name.lower() == 'chatgpt.exe' and executable.parent.name.lower() == 'app'
        and re.fullmatch(r'OpenAI\.Codex_[0-9.]+_x64__2p2nqsd0c76g0', package.name) is not None
        and package.parent == Path(os.environ.get('ProgramFiles', r'C:\Program Files'))/'WindowsApps')
    known = installed_codex or executable == installed or any(executable.is_relative_to(root/'artifacts'/kind)
        for kind in ('original-sync-desktop', 'managed-desktop'))
    if not known:
        return False
    command = process.get('CommandLine') or ''
    matches = re.findall(r'"--user-data-dir=([^"]+)"|--user-data-dir(?:=|\s+)(?:"([^"]+)"|([^\s"]+))', command)
    if not matches:
        return True  # Conservatively require a normal close; never forward.
    return any(Path(next(value for value in values if value)).resolve() == ui.resolve() for values in matches)


def launch(root, *, check=False, prepare_only=False):
    app = find_app()
    original_home = Path.home()/'.codex'
    ui = Path(os.environ['APPDATA'])/'Codex'
    if not (original_home/'config.toml').is_file() or not ui.is_dir():
        raise ValueError('기존 본앱의 설정 경로를 확인하지 못했습니다.')
    installed = Path(app['executable']).resolve()
    running = [p for p in original_processes() if uses_original_ui(p, root, installed, ui)]
    result = dict(original_running=bool(running), requires_normal_close=bool(running),
        original_home=str(original_home), user_data=str(ui), installed_app_unchanged=True)
    if check: return result
    executable = prepare(root,app)
    result['executable'] = str(executable)
    if prepare_only: return result
    if running:
        raise ValueError('본앱이 실행 중입니다. 진행 중인 작업이 끝난 뒤 본앱을 정상 종료하고 이 바로가기를 다시 실행하세요. 강제 종료하지 않습니다.')
    runtime = resolve(root)
    if not all(runtime.get('capabilities', {}).get(name) for name in
               ('shared_append_envelopes', 'shared_history_refresh')):
        raise ValueError('공통 기록 저장 수정이 포함된 관리 런타임을 먼저 적용해야 합니다.')
    if runtime.get('capabilities', {}).get('canonical_record_storage'):
        from manager_core.canonical_storage import migrate
        result['canonical_storage'] = migrate(root)
    result['history_recovery'] = apply_pending(root)
    env = child_environment('original')
    for name in tuple(env):
        if name.upper().startswith(('CODEX_MANAGER_', 'CODEX_RECORD_')):
            env.pop(name)
    env.update(CODEX_HOME=str(original_home), CODEX_ELECTRON_USER_DATA_PATH=str(ui),
        CODEX_RECORD_SIGNALS=str(root/'work/control-center/record-signals'))
    from manager_core.workspace_seed import ensure as seed_ssh_projects
    seed_ssh_projects(root)
    from manager_core.shared_workspaces import prepare_home
    prepare_home(root, original_home)
    env['CODEX_CLI_PATH'] = runtime['runtime']
    env['CODEX_RECORD_SHARED_APPEND'] = '1'
    # Original login/settings stay in place. Both writers now allocate record
    # ordinals from the same durable append protocol, including unknown payloads.
    proc = subprocess.Popen([str(executable),f'--user-data-dir={ui}'],env=env,
        creationflags=subprocess.CREATE_NO_WINDOW)
    result['process_id'] = proc.pid
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[1])
    parser.add_argument('--check',action='store_true')
    parser.add_argument('--prepare',action='store_true')
    parser.add_argument('--notify',action='store_true')
    args=parser.parse_args()
    try:
        result=launch(args.root.resolve(),check=args.check,prepare_only=args.prepare)
        print(json.dumps(result,ensure_ascii=False))
    except Exception as error:
        if args.notify: ctypes.windll.user32.MessageBoxW(None,str(error),'Codex 본앱 동기화',0x10)
        else: print(str(error),file=sys.stderr)
        raise SystemExit(1)
