"""Official Claude CLI authentication; never read, copy or return credentials."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
from uuid import UUID


MINIMUM_VERSION = (2, 1, 282)
_versions = {}
_version_lock = threading.Lock()
_AUTH_PREFIXES = ('ANTHROPIC_', 'OPENAI_', 'AZURE_OPENAI_', 'CHATGPT_', 'DEEPSEEK_', 'CODEX_',
                  'CLAUDE_CODE_', 'CLAUDE_AGENT_', 'CLAUDE_CONFIG_')
_AUTH_NAMES = {'CLAUDECODE', 'CLAUDE_SESSION_ID', 'ENABLE_PROMPT_CACHING_1H',
               'FORCE_PROMPT_CACHING_5M', 'AWS_BEARER_TOKEN_BEDROCK',
               'GOOGLE_APPLICATION_CREDENTIALS', 'DISABLE_COMPACT', 'DISABLE_AUTO_COMPACT'}


class ClaudeError(RuntimeError):
    """Only fixed, non-secret diagnostics may be exposed to the manager."""
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def config_dir(profile_id, environ=None):
    profile_id = str(UUID(str(profile_id)))
    environment = os.environ if environ is None else environ
    base = environment.get('LOCALAPPDATA')
    if not base:
        base = str(Path(environment.get('XDG_CONFIG_HOME') or
                        Path(environment.get('HOME') or Path.home()) / '.config'))
    path = Path(base).expanduser().resolve() / 'codex-multi-provider' / 'claude-profiles' / profile_id
    if path.resolve() != path:
        raise ClaudeError('configuration_path', 'Claude profile configuration must not redirect to another directory.')
    return path


def scrub_environment(directory, environ=None):
    source = os.environ if environ is None else environ
    result = {key: value for key, value in source.items()
              if not key.upper().startswith(_AUTH_PREFIXES) and key.upper() not in _AUTH_NAMES}
    for key, value in source.items():
        if key.upper() == 'CLAUDE_CODE_GIT_BASH_PATH':
            result['CLAUDE_CODE_GIT_BASH_PATH'] = value
    result['CLAUDE_CONFIG_DIR'] = str(directory)
    result['DISABLE_AUTOUPDATER'] = '1'
    return result


LENT_KEYS = frozenset({'profileId', 'accessToken', 'expiresAt', 'accountIdentity'})
# A runtime adds these only when the Windows broker reported them, which it does only for a
# runtime and a prepared helper that both understand them.
SOURCE_KEYS = frozenset({'credentialSource', 'credentialId'})
WINDOWS_LOGIN, LONG_LIVED_TOKEN = 'windowsLogin', 'longLivedToken'


def canonical_uuid(value):
    """True for a lowercase hyphenated UUID string exactly as str(UUID()) prints it."""
    if not isinstance(value, str) or len(value) != 36:
        return False
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False


def borrowed_credential(value, profile_id, account_identity, minimum_expiry):
    """Validate an access-only credential lent by the manager.

    The same rules apply at runner start and to a renewal during a turn. A credential has the
    four original keys, or those and its source: the PC's Windows login (no ID) or the profile's
    saved long-lived token (its credential ID). The error never repeats the received value,
    which may contain a token.
    """
    keys = set(value) if isinstance(value, dict) else None
    if (keys not in (LENT_KEYS, LENT_KEYS | SOURCE_KEYS)
            or value['profileId'] != profile_id or value['accountIdentity'] != account_identity
            or type(value['expiresAt']) is not int or value['expiresAt'] < minimum_expiry
            or not isinstance(value['accessToken'], str) or not 1 <= len(value['accessToken']) <= 65536
            or any(ord(char) <= 32 or ord(char) >= 127 for char in value['accessToken'])):
        raise ValueError('Claude access credential unavailable')
    if keys == LENT_KEYS | SOURCE_KEYS and not (
            (value['credentialSource'] == WINDOWS_LOGIN and value['credentialId'] is None)
            or (value['credentialSource'] == LONG_LIVED_TOKEN and canonical_uuid(value['credentialId']))):
        raise ValueError('Claude access credential unavailable')
    return value


def discover_cli(configured=None, environ=None):
    environment = os.environ if environ is None else environ
    candidates = [configured] if configured else []
    candidates.append(shutil.which('claude', path=environment.get('PATH', '')))
    home = environment.get('USERPROFILE') or environment.get('HOME') or str(Path.home())
    candidates.append(str(Path(home) / '.local' / 'bin' / ('claude.exe' if os.name == 'nt' else 'claude')))
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate).expanduser()
        if (path.is_file() and path.suffix.lower() not in ('.cmd', '.bat', '.ps1')
                and (os.name != 'nt' or path.suffix.lower() == '.exe')):
            return path.resolve()
        if configured and candidate == configured:
            raise ClaudeError('cli_missing', 'Configured native Claude CLI was not found. Install the official native CLI.')
    raise ClaudeError('cli_missing', 'Native Claude CLI was not found. Install the official native CLI on this host.')


def _hidden():
    return {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}


def cli_version(cli_path, environ=None):
    path = Path(cli_path)
    stat = path.stat()
    key = (str(path), stat.st_size, stat.st_mtime_ns)
    with _version_lock:
        cached = _versions.get(key)
    if cached:
        return cached
    try:
        result = subprocess.run([str(path), '--version'], stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                env=environ, timeout=15, **_hidden())
        # Never pass raw CLI diagnostics through to a status card or log.
        match = re.search(rb'\b(\d+)\.(\d+)\.(\d+)\b', result.stdout[:4096])
        if result.returncode or not match:
            raise ClaudeError('cli_version', 'Could not identify the installed Claude CLI version.')
        version = tuple(map(int, match.groups()))
        if version < MINIMUM_VERSION:
            raise ClaudeError('cli_outdated', 'Claude CLI 2.1.282 or newer is required.')
        text = '.'.join(map(str, version))
        with _version_lock:
            _versions[key] = text
        return text
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ClaudeError('cli_version', 'Could not inspect the installed Claude CLI.') from error


def mask_email(value):
    if not isinstance(value, str) or '@' not in value or len(value) > 320:
        return None
    local, domain = value.rsplit('@', 1)
    if not local or not domain or any(ord(c) < 33 for c in value):
        return None
    return local[0] + '***@' + domain


def sanitize_status(raw, *, reveal_email=False):
    """Strict allowlist; full email is only for an explicit transient UI request."""
    if not isinstance(raw, dict) or not isinstance(raw.get('loggedIn'), bool):
        raise ClaudeError('auth_status', 'Claude CLI returned an unsupported authentication status.')
    result = {'logged_in': raw['loggedIn']}
    for source, target in (('authMethod', 'method'), ('apiProvider', 'provider'),
                           ('subscriptionType', 'subscription_type')):
        value = raw.get(source)
        if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_. -]{1,80}', value):
            result[target] = value
    email = mask_email(raw.get('email'))
    if email:
        result['email'] = email
    if reveal_email is True and raw['loggedIn']:
        from .profile_email import email_label
        if full_email := email_label(raw.get('email')):
            result['account_email'] = full_email
    # Account identity is used only as an opaque cache namespace. Cards receive
    # the masked email; token fields never participate or leave the CLI.
    identity = {key: raw[key] for key in ('email', 'orgId', 'authMethod', 'apiProvider')
                if isinstance(raw.get(key), str) and 0 < len(raw[key]) <= 1024}
    if email and identity.get('email'):
        result['account_identity'] = hashlib.sha256(json.dumps(identity, sort_keys=True,
                                                 ensure_ascii=False).encode('utf-8')).hexdigest()
    return result


def auth_status(profile_id, cli_path=None, environ=None, *, reveal_email=False):
    directory = config_dir(profile_id, environ)
    directory.mkdir(parents=True, exist_ok=True)
    environment = scrub_environment(directory, environ)
    cli = discover_cli(cli_path, environ)
    version = cli_version(cli, environment)
    try:
        result = subprocess.run([str(cli), 'auth', 'status', '--json'],
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, env=environment, cwd=directory,
                                timeout=25, **_hidden())
        if result.returncode not in (0, 1) or len(result.stdout) > 65536:
            raise ClaudeError('auth_status', 'Claude CLI authentication status could not be checked.')
        status = sanitize_status(json.loads(result.stdout.decode('utf-8')), reveal_email=reveal_email)
        status.update(cli_version=version, status='ready' if status['logged_in'] else 'not_logged_in')
        return status
    except (ValueError, UnicodeError, OSError, subprocess.TimeoutExpired) as error:
        raise ClaudeError('auth_status', 'Claude CLI authentication status could not be checked.') from error


def launch_login(profile_id, cli_path=None, console=False, environ=None, logout=False):
    """The visible native CLI owns the browser, login URL and all login I/O."""
    directory = config_dir(profile_id, environ)
    directory.mkdir(parents=True, exist_ok=True)
    environment = scrub_environment(directory, environ)
    cli = discover_cli(cli_path, environ)
    cli_version(cli, environment)
    if os.name != 'nt':
        raise ClaudeError('interactive_login_required',
                          'Run Claude auth login interactively on this host with the profile configuration directory.')
    command = [str(cli), 'auth', 'logout' if logout else 'login']
    if not logout:
        command.append('--console' if console else '--claudeai')
    process = subprocess.Popen(command, env=environment, cwd=directory,
                               creationflags=subprocess.CREATE_NEW_CONSOLE, close_fds=True)
    return {'pid': process.pid, 'status': 'logout_started' if logout else 'login_started'}


def usage_directory(profile_id, environ=None):
    """Stable empty cwd avoids accumulating a trusted project for every refresh."""
    parent = config_dir(profile_id, environ)
    path = parent / 'manager-usage'
    if path.resolve() != path:
        raise ClaudeError('configuration_path', 'Claude usage directory must not redirect to another directory.')
    path.mkdir(parents=True, exist_ok=True)
    if any(path.iterdir()):
        raise ClaudeError('configuration_path', 'Claude usage directory must remain empty.')
    return path


def usage_command(cli):
    """The only input is the built-in local usage command; never a model prompt."""
    return [str(cli), '--safe-mode', '--setting-sources', '', '--strict-mcp-config',
            '--mcp-config', '{"mcpServers":{}}', '--tools', '', '--max-turns', '0',
            '--prompt-suggestions', 'false', '--permission-mode', 'dontAsk',
            '--ax-screen-reader', '/usage']


def launch_setup(profile_id, cli_path=None, environ=None):
    """Explicit user click: the visible official CLI owns all setup/login input."""
    if os.name != 'nt':
        raise ClaudeError('interactive_login_required', 'Complete Claude setup interactively on this host.')
    directory = config_dir(profile_id, environ)
    cwd = usage_directory(profile_id, environ)
    environment = scrub_environment(directory, environ)
    environment.update(CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1', TZ='UTC')
    cli = discover_cli(cli_path, environ)
    cli_version(cli, environment)
    # A new visible console must receive its default console standard handles.
    # The hidden ConPTY adapter has a separate explicit-null handle requirement.
    process = subprocess.Popen(usage_command(cli), env=environment, cwd=cwd,
                               creationflags=subprocess.CREATE_NEW_CONSOLE,
                               close_fds=True)
    return dict(pid=process.pid, status='setup_started',
                message='열린 공식 Claude Code 창에서 첫 실행 설정을 완료하고 사용량을 다시 확인해 주세요.')
