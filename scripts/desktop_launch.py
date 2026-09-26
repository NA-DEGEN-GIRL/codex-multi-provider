"""Launch the installed desktop app with an isolated, optional patched runtime.

--check is read-only and never decrypts credentials or launches the desktop app.
The ordinary app, its authentication, and its files are never modified.
"""
import argparse
import ctypes
import functools
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
import tomllib
from urllib.parse import urlsplit
from prepare_profiles import enforce_flash_reasoning

ROOT = Path(__file__).resolve().parents[1]
DESKTOP_HOME = ROOT / 'profiles/desktop-runtime'
DESKTOP_UI = ROOT / 'profiles/desktop-runtime-ui'
RUNTIME = ROOT / 'artifacts/runtime/codex.exe'


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def find_app():
    shell = shutil.which('pwsh.exe') or shutil.which('powershell.exe')
    if not shell:
        raise RuntimeError('PowerShell was not found.')
    command = ("Get-AppxPackage -Name OpenAI.Codex | Sort-Object Version -Descending | "
               "Select-Object -First 1 Name,Version,InstallLocation | ConvertTo-Json -Compress")
    result = subprocess.run([shell, '-NoProfile', '-NonInteractive', '-Command', command],
                            capture_output=True, text=True, encoding='utf-8', timeout=30,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode or not result.stdout.strip():
        raise RuntimeError('The installed Windows Codex package could not be located.')
    app = json.loads(result.stdout)
    app['executable'] = str(Path(app['InstallLocation']) / 'app/ChatGPT.exe')
    if not Path(app['executable']).is_file():
        raise RuntimeError('The installed Codex desktop executable is missing.')
    return app


APP_CACHE_SECONDS = 180
_app_lock = threading.Lock()
_app_cache = None


def _app_stamp(app):
    """Size and mtime of the executable and its app.asar; None if unusable."""
    try:
        executable = Path(app['executable'])
        stamps = []
        for path in (executable, executable.parent / 'resources/app.asar'):
            info = path.stat()
            if not stat.S_ISREG(info.st_mode):
                return None
            stamps.append((str(path), info.st_size, info.st_mtime_ns))
        return tuple(stamps)
    except (OSError, KeyError, TypeError, ValueError):
        return None


@functools.cache
def _package_query():
    from ctypes import wintypes
    query = ctypes.WinDLL('kernel32', use_last_error=True).GetPackagesByPackageFamily
    query.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(wintypes.LPWSTR),
                      ctypes.POINTER(ctypes.c_uint32), wintypes.LPWSTR]
    query.restype = wintypes.LONG
    return query


def _registered_packages(app):
    """Full names registered for this user in the package's family, or None.

    GetPackagesByPackageFamily answers in about a millisecond, so a package
    update or Store auto-update invalidates the cache without PowerShell.
    """
    if os.name != 'nt':
        return None
    parts = Path(str(app.get('InstallLocation', ''))).name.split('_')
    if (len(parts) != 5 or parts[0] != app.get('Name') or parts[1] != str(app.get('Version'))
            or not re.fullmatch(r'[a-z0-9]{13}', parts[4])):
        return None  # Not a standard package folder; stat and age still apply.
    from ctypes import wintypes
    family = parts[0] + '_' + parts[4]
    try:
        query = _package_query()
        count, length = ctypes.c_uint32(0), ctypes.c_uint32(0)
        status = query(family, ctypes.byref(count), None, ctypes.byref(length), None)
        if status not in (0, 122):  # ERROR_INSUFFICIENT_BUFFER reports the sizes.
            return None
        if not count.value:
            return ()
        names = (wintypes.LPWSTR * count.value)()
        buffer = ctypes.create_unicode_buffer(length.value)
        if query(family, ctypes.byref(count), names, ctypes.byref(length), buffer):
            return None  # Changed between calls; the next use asks again.
        return tuple(sorted(names[index] for index in range(count.value)))
    except (AttributeError, OSError, ValueError, ctypes.ArgumentError):
        return None  # Unknown registration never blocks a launch; it only forces a lookup.


def cached_app():
    """find_app() shared across this service while the package is unchanged.

    Reused only while the executable and app.asar keep their size and mtime,
    the user's registered package versions stay the same, and the lookup is
    younger than APP_CACHE_SECONDS. Anything else runs PowerShell again.
    Update checks keep their own uncached package query.
    """
    global _app_cache
    with _app_lock:
        if _app_cache is not None:
            app, stamp, registered, checked = _app_cache
            if (time.monotonic() - checked < APP_CACHE_SECONDS and _app_stamp(app) == stamp
                    and _registered_packages(app) == registered):
                return dict(app)
            _app_cache = None
        app = find_app()
        stamp = _app_stamp(app)
        if stamp is not None:
            _app_cache = (dict(app), stamp, _registered_packages(app), time.monotonic())
        return dict(app)


def forget_app():
    """Drop the shared lookup, e.g. after an update installed another version."""
    global _app_cache
    with _app_lock:
        _app_cache = None


def provider_settings():
    provider = read_json(ROOT / 'profiles/provider.json')
    endpoint = urlsplit(provider.get('base_url', ''))
    if (endpoint.scheme != 'https' or not endpoint.hostname or endpoint.username
            or endpoint.password or endpoint.query or endpoint.fragment):
        raise RuntimeError('Desktop provider settings require an HTTPS base URL without credentials or query parameters.')
    if not isinstance(provider.get('model'), str) or not provider['model'].strip():
        raise RuntimeError('Desktop provider settings require a model ID.')
    return provider


def replace_setting(text, section, key, value):
    """Change one generated TOML field, preserving other user settings/comments."""
    pattern = re.compile(r'^\[' + re.escape(section) + r'\][ \t]*(?:#.*)?$', re.MULTILINE)
    header = pattern.search(text) if section else None
    if section and not header:
        return text.rstrip() + f'\n\n[{section}]\n{key} = {json.dumps(value, ensure_ascii=False)}\n'
    start = header.end() + 1 if header else 0
    following = re.search(r'^\[', text[start:], re.MULTILINE)
    end = start + following.start() if following else len(text)
    body = text[start:end]
    field = re.compile(r'^' + re.escape(key) + r'[ \t]*=.*$', re.MULTILINE)
    line = f'{key} = {json.dumps(value, ensure_ascii=False)}'
    body = field.sub(lambda _: line, body, count=1) if field.search(body) else line + '\n' + body
    return text[:start] + body + text[end:]


def rewrite_home(text, source, destination):
    for old, new in ((str(source), str(destination)),
                     (json.dumps(str(source))[1:-1], json.dumps(str(destination))[1:-1]),
                     (source.as_posix(), destination.as_posix())):
        text = re.sub(re.escape(old), lambda _: new, text, flags=re.IGNORECASE)
    return text


def profile_files(provider, destination=DESKTOP_HOME):
    """Whitelist generated files; authentication, sessions and databases are excluded."""
    source = ROOT / 'profiles/runtime'
    paths = [source / 'config.toml', source / 'deepseek-models.json', *sorted((source / 'agents').glob('*.toml'))]
    if not (source / 'agents/deepseek.toml').is_file():
        raise RuntimeError('The generated CLI profile is incomplete; prepare it in the model lab first.')
    files = {}
    for path in paths:
        text = rewrite_home(path.read_text(encoding='utf-8-sig'), source, destination)
        if path.name == 'config.toml':
            # CLI test directory trust does not belong to this independent app profile.
            text = re.sub(r'^\[projects\.[^\n]+\]\n(?:(?!^\[).*(?:\n|$))*', '', text, flags=re.MULTILINE)
            text = replace_setting(text, '', 'sqlite_home', str(destination / 'sqlite'))
        files[path.relative_to(source)] = text
    return apply_provider(files, provider, destination)


def apply_provider(files, provider, destination=DESKTOP_HOME):
    files = dict(files)
    config = Path('config.toml')
    role = Path('agents/deepseek.toml')
    catalog_path = Path('deepseek-models.json')
    files[config] = replace_setting(files[config], 'model_providers.deepseek_external', 'base_url', provider['base_url'])
    files[role] = replace_setting(files[role], '', 'model', provider['model'])
    files[role] = replace_setting(files[role], '', 'model_catalog_json', str(destination / catalog_path))
    if provider['model'] == 'deepseek-flash':
        files[role] = replace_setting(files[role], '', 'model_reasoning_effort', 'max')
    catalog = json.loads(files[catalog_path])
    catalog['models'][0]['slug'] = provider['model']
    catalog['models'][0]['display_name'] = provider['model'] + ' (external lab)'
    enforce_flash_reasoning(catalog['models'][0])
    files[catalog_path] = json.dumps(catalog, ensure_ascii=False, indent=2) + '\n'
    for path, content in files.items():
        if path.suffix == '.toml':
            tomllib.loads(content)
    return files


def ensure_profile(provider, refresh=False, destination=DESKTOP_HOME):
    created = not (destination / 'config.toml').exists()
    if created:
        # Refuse to overwrite a partially initialized directory or unrelated data.
        if destination.exists() and any(destination.iterdir()):
            raise RuntimeError(f'The desktop profile is incomplete: {destination}. Review it before creating a new profile.')
        files = profile_files(provider, destination)
    elif refresh:
        selected = [Path('config.toml'), Path('agents/deepseek.toml'), Path('deepseek-models.json')]
        files = apply_provider({path: (destination / path).read_text(encoding='utf-8-sig') for path in selected}, provider, destination)
    else:
        # This user policy also applies to existing profiles without replacing app preferences.
        role_path = Path('agents/deepseek.toml')
        role_text = (destination / role_path).read_text(encoding='utf-8-sig')
        if tomllib.loads(role_text).get('model') != 'deepseek-flash':
            return False
        catalog_path = Path('deepseek-models.json')
        catalog = read_json(destination / catalog_path)
        for model_info in catalog['models']:
            enforce_flash_reasoning(model_info)
        files = {role_path: replace_setting(role_text, '', 'model_reasoning_effort', 'max'),
                 catalog_path: json.dumps(catalog, ensure_ascii=False, indent=2) + '\n'}
    for relative, content in files.items():
        path = destination / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists() or path.read_text(encoding='utf-8-sig') != content:
            path.write_text(content, encoding='utf-8')
    return created


def child_environment(mode, key=None):
    env = {name: value for name, value in os.environ.items()
           if not name.upper().startswith('CODEX_') and name.upper() != 'ELECTRON_RUN_AS_NODE'}
    if mode == 'runtime':
        env.update(CODEX_CLI_PATH=str(RUNTIME), CODEX_HOME=str(DESKTOP_HOME),
                   CODEX_ELECTRON_USER_DATA_PATH=str(DESKTOP_UI))
        if key is not None:
            env['CODEX_EXTERNAL_DEEPSEEK_API_KEY'] = key
    return env


def diagnostics(mode):
    app = find_app()
    result = {'mode': mode, 'app_version': app['Version'], 'app_executable': app['executable'],
              'errors': [], 'warnings': [], 'desktop_live_validated': False}
    if mode == 'original':
        result['configuration'] = 'Normal installed app; inherited CODEX_* overrides removed.'
        return result
    provider = provider_settings()
    result.update(runtime=str(RUNTIME), codex_home=str(DESKTOP_HOME), user_data=str(DESKTOP_UI),
                  provider_model=provider['model'], provider_base_url=provider['base_url'],
                  profile_exists=(DESKTOP_HOME / 'config.toml').is_file(),
                  saved_key_exists=(ROOT / 'profiles/deepseek.dpapi').is_file(),
                  isolated_login_exists=(DESKTOP_HOME / 'auth.json').is_file())
    if not result['saved_key_exists']:
        result['errors'].append('Save the DeepSeek API key in the model lab first.')
    for filename in ('codex.exe', 'codex-code-mode-host.exe', 'codex-command-runner.exe', 'codex-windows-sandbox-setup.exe'):
        if not (RUNTIME.parent / filename).is_file():
            result['errors'].append(f'Missing runtime companion: {filename}')
    if RUNTIME.is_file():
        with RUNTIME.open('rb') as binary:
            digest = hashlib.file_digest(binary, 'sha256').hexdigest()
        info = read_json(RUNTIME.parent / 'build-info.json')
        result.update(runtime_version=info['version'], runtime_sha256=digest)
        if digest != info.get('sha256') or not info.get('external_bridge_present'):
            result['errors'].append('The patched runtime does not match its recorded build metadata.')
    if result['profile_exists']:
        config = tomllib.loads((DESKTOP_HOME / 'config.toml').read_text(encoding='utf-8-sig'))
        role = tomllib.loads((DESKTOP_HOME / 'agents/deepseek.toml').read_text(encoding='utf-8-sig'))
        active_model = role.get('model')
        active_url = config.get('model_providers', {}).get('deepseek_external', {}).get('base_url')
        result['active_profile_model'] = active_model
        if active_model != provider['model'] or active_url != provider['base_url']:
            result['warnings'].append('저장한 공급자 설정과 기존 앱 프로필이 다릅니다. 실험 앱을 닫고 --refresh-provider로 실행하면 변경한 주소와 모델이 반영됩니다.')
    else:
        profile_files(provider)  # Validate proposed TOML without creating any files.
    if not result['isolated_login_exists']:
        result['warnings'].append('실험 앱에서 ChatGPT에 한 번 로그인해야 합니다. 기존 앱의 로그인 정보는 복사하지 않습니다.')
    result['warnings'].append('이미 실행 중인 앱은 현재 런타임을 유지합니다. 공급자 설정을 변경했다면 실험 앱을 닫고 다시 실행하세요.')
    return result


def verify_runtime_startup(process, timeout=6):
    """Confirm only the requested app's direct patched child, without changing it."""
    shell = shutil.which('pwsh.exe') or shutil.which('powershell.exe')
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return {'state': 'forwarded_or_exited', 'exit_code': process.returncode}
        command = (f'Get-CimInstance Win32_Process -Filter "ParentProcessId = {process.pid} AND Name = \'codex.exe\'" | '
                   'Select-Object ProcessId,ExecutablePath | ConvertTo-Json -Compress')
        try:
            query = subprocess.run([shell, '-NoProfile', '-NonInteractive', '-Command', command],
                                   capture_output=True, text=True, encoding='utf-8', timeout=3,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
            rows = json.loads(query.stdout) if query.returncode == 0 and query.stdout.strip() else []
            if isinstance(rows, dict):
                rows = [rows]
            for row in rows:
                if os.path.normcase(row.get('ExecutablePath') or '') == os.path.normcase(str(RUNTIME)):
                    return {'state': 'verified', 'app_process_id': process.pid,
                            'runtime_process_id': row['ProcessId'], 'runtime': str(RUNTIME)}
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
            pass
        time.sleep(0.4)
    return {'state': 'pending', 'app_process_id': process.pid}


def launch(mode, refresh=False):
    status = diagnostics(mode)
    if status['errors']:
        raise RuntimeError('; '.join(status['errors']))
    key = None
    if mode == 'runtime':
        # Import the existing DPAPI helper only during launch, never during --check.
        sys.dont_write_bytecode = True
        from live_test import decrypt_key
        key = decrypt_key()
        status['profile_created'] = ensure_profile(provider_settings(), refresh)
        DESKTOP_UI.mkdir(parents=True, exist_ok=True)
    env = child_environment(mode, key)
    key = None
    command = [status['app_executable']]
    if mode == 'runtime':
        # The Owl/Chromium shell acquires its native process singleton before
        # Electron JavaScript can apply CODEX_ELECTRON_USER_DATA_PATH.
        command.append(f'--user-data-dir={DESKTOP_UI}')
    try:
        process = subprocess.Popen(command, env=env, cwd=ROOT,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL,
                                   creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    finally:
        env.pop('CODEX_EXTERNAL_DEEPSEEK_API_KEY', None)
    status.update(status='launch_requested', launcher_process_id=process.pid)
    if mode == 'runtime':
        status['runtime_startup'] = verify_runtime_startup(process)
        if status['runtime_startup']['state'] == 'forwarded_or_exited':
            status['warnings'].append('실행 요청 프로세스가 종료되었습니다. 기존 실험 창으로 전달되었을 수 있습니다. 새 패치 앱 서버의 시작을 이번 요청에서 확인하지는 못했습니다.')
        elif status['runtime_startup']['state'] == 'pending':
            status['warnings'].append('실험 앱 프로세스는 실행 중이며, 패치 앱 서버 시작은 아직 확인 대기 중입니다. 앱에 표시된 상태를 확인하세요.')
    output = ROOT / 'work/desktop-launch-last.json'
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding='utf-8')
    return status


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=('runtime', 'original'), default='runtime')
    parser.add_argument('--check', action='store_true', help='Read-only diagnostics; no GUI, configuration writes, or key decryption.')
    parser.add_argument('--refresh-provider', action='store_true', help='Update only the external provider URL, role model and model catalog in the existing experimental profile.')
    parser.add_argument('--notify', action='store_true', help='Show a Windows error dialog when launched without a console.')
    args = parser.parse_args()
    try:
        if os.name != 'nt':
            raise RuntimeError('This launcher requires Windows.')
        result = diagnostics(args.mode) if args.check else launch(args.mode, args.refresh_provider)
        if sys.stdout:
            if args.check:
                print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
            else:
                label = '패치 런타임을 사용하는 별도 실험 앱' if args.mode == 'runtime' else '기본 Windows Codex 앱'
                print(f'{label}을 열도록 요청했습니다. (프로세스 {result["launcher_process_id"]})', flush=True)
                if args.mode == 'runtime':
                    startup = result.get('runtime_startup', {})
                    if startup.get('state') == 'verified':
                        print(f'패치 앱 서버 실행 확인: 프로세스 {startup["runtime_process_id"]}', flush=True)
                    print(f'앱 전용 작업·설정 폴더: {DESKTOP_HOME}', flush=True)
                    print('실험 창에서 새 작업을 시작하세요. 기본 앱으로 돌아가려면 기존 시작 메뉴의 앱이나 실험실의 기본 앱 버튼을 사용하세요.', flush=True)
                for warning in result['warnings']:
                    print(warning, flush=True)
                print(f'실행 진단: {ROOT / "work/desktop-launch-last.json"}', flush=True)
        return 1 if result.get('errors') else 0
    except Exception as error:
        message = str(error)
        if sys.stderr:
            print(message, file=sys.stderr, flush=True)
        if args.notify:
            ctypes.windll.user32.MessageBoxW(None, message, 'Codex experimental launcher', 0x10)
        return 1


if __name__ == '__main__':
    if sys.stdout:
        sys.stdout.reconfigure(encoding='utf-8')
    if sys.stderr:
        sys.stderr.reconfigure(encoding='utf-8')
    sys.exit(main())
