"""Run the unmodified Claude CLI for one native task turn over JSONL v1.

The manager owns only this ledger and IPC metadata. Claude owns authentication
and sessions. The only Claude file inspected is this run's exact UUID transcript,
bounded and confined to projects/, for a verified portable compact summary.
"""
import argparse
from contextlib import contextmanager, nullcontext
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from uuid import UUID, uuid4
from multiprocessing import AuthenticationError
from multiprocessing.connection import Listener

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from manager_core.claude_auth import (ClaudeError, auth_status, config_dir,
                                      discover_cli, scrub_environment)
from manager_core.claude_protocol import (MAX_LINE_BYTES, ProtocolError, bounded_text, compact_summary_fits,
                                          encode_message, normalize, read_message, usage_fields)
from manager_core.claude_profiles import MODEL_EFFORTS, automatic_context_window
from manager_core.store import Store, atomic_json


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


class StdinLines:
    """Keep a daemon reader out of BufferedReader's interpreter-shutdown lock."""
    def __init__(self, descriptor):
        self.descriptor = descriptor
        self.buffer = bytearray()

    def readline(self, limit):
        while True:
            newline = self.buffer.find(b'\n', 0, limit)
            if newline >= 0:
                count = newline + 1
                value = bytes(self.buffer[:count])
                del self.buffer[:count]
                return value
            if len(self.buffer) >= limit:
                value = bytes(self.buffer[:limit])
                del self.buffer[:limit]
                return value
            chunk = os.read(self.descriptor, min(65536, limit - len(self.buffer)))
            if not chunk:
                value = bytes(self.buffer)
                self.buffer.clear()
                return value
            self.buffer.extend(chunk)


def run_settings(value):
    model = value.get('model', 'opus')
    if not isinstance(model, str):
        raise ClaudeError('invalid_settings', 'Unsupported Claude model.')
    model = model.removeprefix('cc-')
    if model not in MODEL_EFFORTS:
        raise ClaudeError('invalid_settings', 'Unsupported Claude model.')
    effort = value.get('effort', value.get('reasoning_effort', 'high'))
    if effort not in MODEL_EFFORTS[model]:
        raise ClaudeError('invalid_settings', 'Unsupported Claude effort.')
    context = value.get('context_window')
    percent = value.get('auto_compact_percent', value.get('autocompact_percent'))
    if context is not None and (type(context) is not int or not 100000 <= context <= 1000000):
        raise ClaudeError('invalid_settings', 'Claude context window must be between 100000 and 1000000 tokens.')
    threshold = None
    if percent is not None:
        if not isinstance(percent, int) or isinstance(percent, bool) or not 1 <= percent <= 100:
            raise ClaudeError('invalid_settings', 'Claude auto-compaction percentage must be between 1 and 100.')
        threshold = (context or automatic_context_window(model)) * percent // 100
        if not 100000 <= threshold <= 1000000:
            raise ClaudeError('invalid_settings', 'Claude auto-compaction threshold must be at least 100000 tokens.')
    return dict(model=model, effort=effort, context_window=context,
                auto_compact_percent=percent, autocompact_tokens=threshold,
                exclude_dynamic_sections=bool(value.get('exclude_dynamic_sections', True)), prompt_version=1)


def system_prompt_text(value):
    if not isinstance(value, str) or len(value.encode('utf-8')) > 80000:
        raise ClaudeError('instructions_size', 'Effective project instructions exceed the 80000-byte Claude bridge limit.')
    return value


def private_temporary_directory(prefix):
    """Create IPC/instruction scratch space accessible only to its owner/system."""
    temporary = tempfile.TemporaryDirectory(prefix=prefix)
    try:
        if os.name == 'nt':
            import ctypes
            from ctypes import wintypes
            security = ctypes.WinDLL('advapi32', use_last_error=True)
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            convert = security.ConvertStringSecurityDescriptorToSecurityDescriptorW
            convert.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p),
                                ctypes.POINTER(wintypes.DWORD)]
            convert.restype = wintypes.BOOL
            apply = security.SetFileSecurityW
            apply.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
            apply.restype = wintypes.BOOL
            kernel.LocalFree.argtypes = [ctypes.c_void_p]
            kernel.LocalFree.restype = ctypes.c_void_p
            descriptor = ctypes.c_void_p()
            # A protected, inheritable DACL. OW means the object's owner; no
            # inherited Users/Everyone grants from a customized temp directory.
            if not convert('D:P(A;OICI;FA;;;OW)(A;OICI;FA;;;SY)', 1,
                           ctypes.byref(descriptor), None):
                raise OSError('Could not create a private temporary directory.')
            try:
                if not apply(temporary.name, 0x80000004, descriptor):
                    raise OSError('Could not protect a private temporary directory.')
            finally:
                kernel.LocalFree(descriptor)
        else:
            os.chmod(temporary.name, 0o700)
        return temporary
    except BaseException:
        temporary.cleanup()
        raise


def validate_hello(message, profile_id):
    if message.get('type') != 'hello' or message.get('protocol') != 1:
        raise ClaudeError('protocol', 'Claude runner requires JSONL protocol 1 hello.')
    if message.get('claude_profile_id') != profile_id:
        raise ClaudeError('profile_mismatch', 'Claude profile does not match the configured runner.')
    for key in ('thread_id', 'turn_id'):
        if not isinstance(message.get(key), str) or not 1 <= len(message[key]) <= 160:
            raise ClaudeError('protocol', 'Task and turn identifiers are required.')
    raw = message.get('cwd')
    if not isinstance(raw, str) or raw.startswith(('ssh:', 'remote:', '\\\\', '//')):
        raise ClaudeError('remote_cwd', 'Claude must run on the task host with a locally authenticated CLI.')
    cwd = Path(raw)
    if not cwd.is_absolute() or not cwd.is_dir():
        raise ClaudeError('remote_cwd', 'Claude task directory is not available on this host.')
    if message.get('trusted_cwd') is not True:
        raise ClaudeError('untrusted_cwd', 'Trust this project in Codex before starting Claude.')
    snapshot = message.get('snapshot')
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get('turn_ids'), list):
        raise ClaudeError('protocol', 'A canonical task history snapshot is required.')
    if (len(snapshot['turn_ids']) > 100000 or any(not isinstance(item, str) or len(item) > 160
                                               for item in snapshot['turn_ids'])):
        raise ClaudeError('protocol', 'Invalid task history snapshot.')
    fingerprints = snapshot.get('turn_fingerprints', {})
    if not isinstance(fingerprints, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                                               for k, v in fingerprints.items()):
        raise ClaudeError('protocol', 'Invalid task history fingerprints.')
    return cwd.resolve()


def instruction_fingerprint(cwd):
    """Invalidate cached instruction snapshots when project instructions change."""
    entries = {}
    for index, directory in enumerate((cwd, *cwd.parents)):
        if index > 32:
            break
        for name in ('AGENTS.md', 'CLAUDE.md', '.claude/CLAUDE.md'):
            path = directory / name
            if path.is_file():
                if path.is_symlink() or path.resolve().name in ('.credentials.json', 'auth.json'):
                    raise ClaudeError('instructions_path', 'Project instruction files must not redirect to private files.')
                if path.stat().st_size > 2 * 1024 * 1024:
                    raise ClaudeError('instructions_size', 'Project instructions exceed the Claude bridge size limit.')
                entries[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
        if (directory / '.git').exists():
            break
    return digest(entries)


@contextmanager
def session_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise ClaudeError('busy', 'This Claude profile is already running this task.') from error
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def session_identity(settings):
    # Model/effort are CLI options on --resume, not a new conversation. The
    # token threshold is derived from the model plus the retained user limits.
    return {key: value for key, value in settings.items()
            if key not in ('model', 'effort', 'autocompact_tokens')}


class SessionLedger:
    def __init__(self, directory, profile_id, thread_id, settings, cwd):
        self.directory = directory / 'sessions' / profile_id / digest(thread_id)
        if not self.directory.resolve().is_relative_to(directory.resolve()):
            raise ClaudeError('ledger_path', 'Claude session metadata must stay in its manager directory.')
        self.cwd = str(cwd)
        self.identity = session_identity(settings)
        self.path = self.directory / ('session-' + digest(dict(settings=self.identity, cwd=self.cwd)) + '.json')
        # One profile/task lock across settings prevents two buckets editing the same task together.
        self.lock_path = self.directory / 'run.lock'

    def load(self, snapshot):
        try:
            path = self.path
            if not path.exists():
                # Migrate the latest compatible old settings bucket. Selecting
                # before validation prevents reviving an older clean session
                # after interruption, rollback, or an edited canonical answer.
                candidates = []
                for index, candidate in enumerate(self.directory.glob('*.json')):
                    if index >= 512:
                        return None
                    if not re.fullmatch(r'[0-9a-f]{64}\.json', candidate.name):
                        continue
                    if candidate.is_symlink() or candidate.stat().st_size > MAX_LINE_BYTES:
                        return None
                    old = json.loads(candidate.read_text(encoding='utf-8'))
                    if not isinstance(old, dict):
                        return None
                    old_settings = old.get('settings')
                    if not isinstance(old_settings, dict) or session_identity(old_settings) != self.identity:
                        continue
                    expected = digest(dict(settings=old_settings, cwd=self.cwd)) + '.json'
                    if candidate.name == expected:
                        candidates.append((candidate.stat().st_mtime_ns, candidate.name, candidate))
                if not candidates:
                    return None
                newest = max(candidate[0] for candidate in candidates)
                latest = [candidate[2] for candidate in candidates if candidate[0] == newest]
                # Equal timestamps cannot prove which session ran last. A
                # filename ordering must never revive a possibly older bucket.
                if len(latest) != 1:
                    return None
                path = latest[0]
            if path.is_symlink() or path.stat().st_size > MAX_LINE_BYTES:
                return None
            record = json.loads(path.read_text(encoding='utf-8'))
            UUID(record['id'])
            if record.get('dirty') or not set(record['covered']).issubset(snapshot['turn_ids']):
                return None
            old, current = record.get('turn_fingerprints', {}), snapshot.get('turn_fingerprints', {})
            if old:
                if any(current.get(turn) != fingerprint for turn, fingerprint in old.items()):
                    return None
            elif record.get('fingerprint') != snapshot.get('fingerprint'):
                return None
            return record
        except (OSError, ValueError, TypeError, KeyError):
            return None

    def save(self, record):
        atomic_json(self.path, record)


def compact_summary(directory, session_id):
    """Read only <projects>/<project>/<exact-UUID>.jsonl, at most 8 MiB per file."""
    session_id = str(UUID(session_id))
    projects = directory / 'projects'
    if not projects.is_dir() or projects.is_symlink():
        return None
    base = projects.resolve()
    if not base.is_relative_to(directory.resolve()):
        return None
    try:
        for index, project in enumerate(projects.iterdir()):
            if index >= 4096:
                break
            if not project.is_dir() or project.is_symlink():
                continue
            path = project / (session_id + '.jsonl')
            if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(base):
                continue
            with path.open('rb') as source:
                source.seek(0, os.SEEK_END)
                size = source.tell()
                source.seek(max(0, size - MAX_LINE_BYTES))
                if size > MAX_LINE_BYTES:
                    source.readline(MAX_LINE_BYTES)
                tail = source.read(MAX_LINE_BYTES)
            latest = None
            for line in tail.splitlines():
                if len(line) > MAX_LINE_BYTES:
                    continue
                try:
                    record = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if not isinstance(record, dict) or record.get('isCompactSummary') is not True:
                    continue
                if record.get('sessionId') not in (None, session_id):
                    continue
                message = record.get('message')
                if not isinstance(message, dict):
                    continue
                content = message.get('content')
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    text = '\n'.join(block.get('text', '') for block in content
                                     if isinstance(block, dict) and block.get('type') == 'text'
                                     and isinstance(block.get('text'), str))
                else:
                    continue
                if text:
                    latest = text if compact_summary_fits(text) else None
            return latest
    except OSError:
        return None
    return None


class ProcessTree:
    """Windows Job Object closes descendants; POSIX uses a fresh process group."""
    def __init__(self, process):
        self.process, self.job = process, None
        if os.name != 'nt':
            return
        import ctypes
        from ctypes import wintypes as w
        class Basic(ctypes.Structure):
            _fields_ = [('PerProcessUserTimeLimit', ctypes.c_longlong), ('PerJobUserTimeLimit', ctypes.c_longlong),
                        ('LimitFlags', w.DWORD), ('MinimumWorkingSetSize', ctypes.c_size_t),
                        ('MaximumWorkingSetSize', ctypes.c_size_t), ('ActiveProcessLimit', w.DWORD),
                        ('Affinity', ctypes.c_size_t), ('PriorityClass', w.DWORD), ('SchedulingClass', w.DWORD)]
        class Counters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in ('ReadOperationCount', 'WriteOperationCount',
                        'OtherOperationCount', 'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]
        class Extended(ctypes.Structure):
            _fields_ = [('BasicLimitInformation', Basic), ('IoInfo', Counters),
                        ('ProcessMemoryLimit', ctypes.c_size_t), ('JobMemoryLimit', ctypes.c_size_t),
                        ('PeakProcessMemoryUsed', ctypes.c_size_t), ('PeakJobMemoryUsed', ctypes.c_size_t)]
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.kernel.CreateJobObjectW.restype = w.HANDLE
        self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
        self.kernel.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
        self.kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        self.kernel.CloseHandle.argtypes = [w.HANDLE]
        self.job = self.kernel.CreateJobObjectW(None, None)
        info = Extended()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if (not self.job or not self.kernel.SetInformationJobObject(self.job, 9, ctypes.byref(info), ctypes.sizeof(info))
                or not self.kernel.AssignProcessToJobObject(self.job, int(process._handle))):
            self.close()
            process.kill()
            raise ClaudeError('process_isolation', 'Could not isolate the Claude process tree.')

    def close(self):
        if self.job:
            self.kernel.CloseHandle(self.job)
            self.job = None
        elif os.name != 'nt':
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


class PermissionBridge:
    def __init__(self, emit, stopped, mode, observed):
        self.emit, self.stopped, self.mode, self.observed = emit, stopped, mode, observed
        self.pending = {}
        self.guard = threading.Lock()
        self.key = os.urandom(32)
        self.temp = private_temporary_directory('codex-claude-')
        self.family = 'AF_PIPE' if os.name == 'nt' else 'AF_UNIX'
        self.address = (r'\\.\pipe\codex-claude-' + str(uuid4()) if os.name == 'nt'
                        else str(Path(self.temp.name) / 'approval.sock'))
        self.listener = Listener(self.address, family=self.family, authkey=self.key)
        self.thread = threading.Thread(target=self._accept, daemon=True)
        self.thread.start()

    def configuration(self):
        return {'mcpServers': {'codex_bridge': {'command': sys.executable,
                'args': ['-X', 'utf8', str(Path(__file__).with_name('claude_permission_mcp.py'))],
                'env': {'CLAUDE_BRIDGE_ADDRESS': self.address, 'CLAUDE_BRIDGE_FAMILY': self.family,
                        'CLAUDE_BRIDGE_KEY': self.key.hex()}}}}

    def _accept(self):
        while not self.stopped.is_set():
            try:
                connection = self.listener.accept()
            except AuthenticationError:
                continue
            except (OSError, EOFError):
                return
            threading.Thread(target=self._handle, args=(connection,), daemon=True).start()

    def _handle(self, connection):
        with connection:
            try:
                request = json.loads(connection.recv_bytes(MAX_LINE_BYTES))
                answer = self.ask(request)
                connection.send_bytes(json.dumps(answer, ensure_ascii=False).encode('utf-8'))
            except (OSError, EOFError, ValueError):
                return

    def ask(self, request):
        deny = {'behavior': 'deny', 'message': 'This action has no matching native approval.'}
        if not isinstance(request, dict) or not isinstance(request.get('input'), dict):
            return deny
        tool, tool_input, tool_id = request.get('tool_name'), request['input'], request.get('tool_use_id')
        if not isinstance(tool, str) or tool.startswith('mcp__codex_bridge__'):
            return deny
        if self.mode == 'plan' and tool not in ('Read', 'Glob', 'Grep', 'LS', 'WebFetch', 'WebSearch'):
            return {'behavior': 'deny', 'message': 'This read-only task cannot approve a potentially mutating tool.'}
        # Public MCP permission requests are tied to an observed tool invocation.
        # A model calling approve directly must never manufacture its own permission.
        deadline = time.monotonic() + 2
        while not self.stopped.is_set():
            with self.guard:
                matches = [value for key, value in self.observed.items()
                           if (not tool_id or key == tool_id) and value == (tool, tool_input)]
            if len(matches) == 1:
                break
            if time.monotonic() >= deadline:
                return deny
            self.stopped.wait(.02)
        if self.stopped.is_set():
            return deny
        request_id, arrived = str(uuid4()), threading.Event()
        slot = {'event': arrived, 'decision': None}
        with self.guard:
            self.pending[request_id] = slot
        self.emit(dict(type='permission_request', id=request_id, tool=tool, input=tool_input,
                       summary='Claude requests permission to use ' + tool))
        while not self.stopped.is_set() and not arrived.wait(.1):
            pass
        with self.guard:
            self.pending.pop(request_id, None)
        decision = slot['decision']
        if not self.stopped.is_set() and decision and decision.get('behavior') == 'allow':
            return {'behavior': 'allow', 'updatedInput': tool_input}
        return {'behavior': 'deny', 'message': bounded_text((decision or {}).get('message'), 2000)
                or 'The user declined or interrupted this action.'}

    def decide(self, decision):
        if decision.get('behavior') not in ('allow', 'deny'):
            return
        with self.guard:
            slot = self.pending.get(decision.get('id'))
            if slot and slot['decision'] is None:
                slot['decision'] = decision
                slot['event'].set()

    def close(self):
        self.stopped.set()
        self.listener.close()
        self.temp.cleanup()


def build_command(cli, session, resume, settings, mode, prompts, directory, manager_root, plugin_dirs,
                  mcp_path=None, system_prompt_path=None, managed_delegation=False):
    if mode not in ('plan', 'acceptEdits', 'auto') or prompts not in ('none', 'host'):
        raise ClaudeError('permissions', 'Unsupported Claude permission policy.')
    if prompts == 'host' and mcp_path is None:
        raise ClaudeError('permissions', 'Claude native approval bridge is required.')
    deny = ['Read(**/.credentials.json)', 'Read(**/auth.json)',
            'Read(//' + directory.parent.as_posix().lstrip('/') + '/**)',
            'Read(//' + (manager_root / 'work/control-center/profiles').as_posix().lstrip('/') + '/**/auth.json)']
    if managed_delegation:
        deny += ['Agent', 'Task']
    permissions = {'deny': deny}
    if settings['effort'] == 'ultracode' and prompts == 'none':
        # Ultracode works through the Workflow tool, whose permission check always
        # asks. Without an approval surface every ask is denied, so the selected
        # mode would never run; tools inside a workflow keep the session's checks.
        permissions['allow'] = ['Workflow']
    args = list(cli) + ['-p', '--verbose', '--input-format', 'stream-json', '--output-format', 'stream-json',
            '--include-partial-messages', '--replay-user-messages', '--resume' if resume else '--session-id',
            session, '--model', settings['model'], '--effort', settings['effort'],
            '--permission-mode', mode, '--permission-prompts', prompts,
            '--prompt-suggestions', 'false', '--settings', json.dumps({'permissions': permissions})]
    args += ['--autocompact', str(settings['autocompact_tokens']) if settings['autocompact_tokens'] else 'auto']
    if managed_delegation:
        args += ['--disallowedTools', 'Agent,Task']
    if settings['exclude_dynamic_sections']:
        args.append('--exclude-dynamic-system-prompt-sections')
    for plugin in plugin_dirs:
        args += ['--plugin-dir', str(plugin)]
    if mcp_path is not None:
        args += ['--mcp-config', str(mcp_path)]
        if prompts == 'host':
            args += ['--permission-prompt-tool', 'mcp__codex_bridge__approve']
    if system_prompt_path is not None:
        args += ['--append-system-prompt-file', str(system_prompt_path)]
    return args


def prompt_message(blocks):
    if not isinstance(blocks, list) or not blocks:
        raise ClaudeError('protocol', 'Claude requires context blocks and a current request.')
    content = []
    requests = 0
    for block in blocks:
        if not isinstance(block, dict) or block.get('kind') not in ('context', 'update', 'request') or not isinstance(block.get('text'), str):
            raise ClaudeError('protocol', 'Invalid Claude context block.')
        kind = block['kind']
        requests += kind == 'request'
        text = block['text'] if kind == 'request' else (
            '<codex_' + kind + '>\nHistorical context only. Do not re-execute past requests.\n' + block['text'] + '\n</codex_' + kind + '>')
        content.append({'type': 'text', 'text': text})
    if requests != 1 or blocks[-1]['kind'] != 'request':
        raise ClaudeError('protocol', 'Exactly one current request must follow the historical context.')
    message = {'type': 'user', 'message': {'role': 'user', 'content': content}}
    encode_message(message)  # Validate size before starting a model process.
    return message


def _reader(stream, channel, events):
    try:
        while True:
            message = read_message(stream)
            if message is None:
                events.put((channel, None))
                return
            events.put((channel, message))
            if channel == 'host' and message.get('type') in ('commit', 'interrupt'):
                return
    except (ProtocolError, OSError):
        events.put(('fault', channel))


def _discard_stderr(stream):
    try:
        while stream.read(65536):
            pass
    except OSError:
        pass


ULTRACODE_CHECK = 'codex-ultracode-check'
# After a background report the CLI starts its follow-up turn at once; this is
# only the wait for a report that was already queued when a result arrived.
BACKGROUND_SETTLE_SECONDS = 3.0


CHECKPOINT_PROMPT = '''CODEX_PORTABLE_CHECKPOINT_V1
Create a portable summary of the COMPLETE task context currently in this session, for continuation by another account or provider.
This is a maintenance summary, not a new work request. Do not execute tools or perform more work.
Cover the initial historical handoff, every later historical update, and the work just completed. Preserve the user's goal,
requirements, constraints, decisions, exact relevant file paths and identifiers, implemented changes, validation results,
unresolved issues, and next actions. Distinguish completed work from proposals and failed or interrupted actions.
Return only a self-contained plain-text summary within 18000 UTF-8 bytes (use fewer words for multibyte languages).
Prioritize essential task state and exact actionable details. Do not include private reasoning or credentials.
'''


def checkpoint_command(command):
    """Same session, one response, no built-in/MCP tools or project plugins."""
    result = []
    index = 0
    while index < len(command):
        value = command[index]
        if value in ('--plugin-dir', '--mcp-config', '--permission-prompt-tool', '--disallowedTools'):
            index += 2
            continue
        if value in ('--session-id', '--resume'):
            result += ['--resume', command[index + 1]]
            index += 2
            continue
        if value in ('--permission-mode', '--permission-prompts'):
            result += [value, 'plan' if value == '--permission-mode' else 'none']
            index += 2
            continue
        result.append(value)
        index += 1
    return result + ['--safe-mode', '--tools', '', '--disallowedTools', '*', '--strict-mcp-config',
                     '--mcp-config', '{"mcpServers":{}}', '--max-turns', '1']


def sum_usage(*values):
    result = {}
    for value in values:
        for key, number in usage_fields(value).items():
            if key == 'cache_creation':
                target = result.setdefault(key, {})
                for duration, count in number.items():
                    target[duration] = target.get(duration, 0) + count
            else:
                result[key] = result.get(key, 0) + number
    return result


def generate_checkpoint(command, cwd, environment, events, session_id):
    """One bounded maintenance call; never recursively summarizes itself.

The primary call's permission stop event is already set. Cancellation for this
independent child comes directly from the existing native-input event queue.
"""
    options = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {'start_new_session': True}
    process = subprocess.Popen(checkpoint_command(command), cwd=cwd, env=environment, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, **options)
    tree, result, cancelled, invalid = None, {}, False, False
    compactions, last_usage, limit_event = 0, {}, None
    try:
        tree = ProcessTree(process)
        threading.Thread(target=_reader, args=(process.stdout, 'checkpoint', events), daemon=True).start()
        threading.Thread(target=_discard_stderr, args=(process.stderr,), daemon=True).start()
        process.stdin.write(encode_message(prompt_message([{'kind':'request', 'text':CHECKPOINT_PROMPT}])))
        process.stdin.flush()
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            try:
                source, message = events.get(timeout=.2)
            except queue.Empty:
                if process.poll() is not None:
                    break
                continue
            if source == 'host':
                if message is None or message.get('type') == 'interrupt':
                    cancelled = True
                    break
                # No permission request can originate from this tool-free call.
                if message.get('type') != 'permission_decision':
                    invalid = True
                    break
                continue
            if source == 'fault':
                invalid = True
                break
            if source != 'checkpoint':
                continue  # The main call may have queued its final EOF.
            if message is None:
                break
            if message.get('type') == 'control_request':
                invalid = True
                break
            if message.get('type') == 'rate_limit_event' and usage_store is not None:
                normalized = normalize(message)
                limit_event = normalized[0] if normalized else None
                if limit_event and limit_event.get('errorCode') == 'credits_required':
                    invalid = True
                    break
            if message.get('type') == 'system' and message.get('subtype') == 'init':
                if message.get('session_id') != session_id:
                    invalid = True
                    break
            if message.get('type') == 'system' and message.get('subtype') == 'compact_boundary':
                compactions += 1  # Count, but never start a third model call.
            if message.get('type') == 'assistant' and isinstance(message.get('message'), dict):
                last_usage = usage_fields(message['message'].get('usage')) or last_usage
                if any(event['kind'] == 'tool_start' for event in normalize(message)):
                    invalid = True
                    break
            if message.get('type') == 'result':
                result = message
                break
    finally:
        try:
            process.stdin.close()
        except OSError:
            pass
        try:
            process.wait(timeout=5 if result else 1)
        except subprocess.TimeoutExpired:
            if tree:
                tree.close()
            else:
                process.kill()
            process.wait(timeout=5)
        if tree:
            tree.close()
        process.stdout.close()
        process.stderr.close()
    text = result.get('result')
    valid = (not cancelled and not invalid and process.returncode == 0 and result.get('is_error') is False
             and compact_summary_fits(text) and bool(text.strip())
             and result.get('session_id') in (None, session_id))
    return dict(success=valid, cancelled=cancelled, summary=text if valid else None, result=result,
                compactions=compactions, last_usage=last_usage, limit_event=limit_event)


def execute(command, cwd, environment, incoming, emit, bridge, stopped, observed, record, ledger, hello,
            configuration_directory, usage_store, delegation=None):
    start = time.monotonic()
    result, problem, interrupted = None, None, False
    record['dirty'] = True  # A runner crash after launch must never resume blindly.
    ledger.save(record)
    prior_summary = compact_summary(configuration_directory, record['id'])
    options = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {'start_new_session': True}
    process = subprocess.Popen(command, cwd=cwd, env=environment, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, **options)
    tree = None
    events = queue.Queue(maxsize=256)
    last_usage, model_started, compactions = {}, False, 0
    prior_cost = record.get('cost_total_usd', 0)
    # Workflows and background agents report back after the turn's first
    # result; the CLI then starts the next turn by itself. The final result
    # is the one that arrives while none of them is still running.
    background, background_seen, settle_deadline, results = set(), False, None, []
    try:
        tree = ProcessTree(process)
        threading.Thread(target=_reader, args=(process.stdout, 'cli', events), daemon=True).start()
        threading.Thread(target=_reader, args=(incoming, 'host', events), daemon=True).start()
        threading.Thread(target=_discard_stderr, args=(process.stderr,), daemon=True).start()
        process.stdin.write(encode_message(hello['_prompt']))
        if record['settings'].get('effort') == 'ultracode':
            # The CLI accepts Ultracode silently and applies it only where dynamic
            # workflows are available; ask which settings it actually applied.
            process.stdin.write(encode_message({'type': 'control_request', 'request_id': ULTRACODE_CHECK,
                                                'request': {'subtype': 'get_settings'}}))
        process.stdin.flush()
        while True:
            if settle_deadline is not None and time.monotonic() >= settle_deadline:
                break
            try:
                source, message = events.get(timeout=.2)
            except queue.Empty:
                if process.poll() is not None:
                    if result is not None:
                        break
                    problem = ('cli_exit', 'Claude exited without a final result.')
                    break
                continue
            if source == 'fault':
                problem = ('protocol', 'Claude bridge received malformed or oversized output.')
                break
            if source == 'host':
                if message is None or message.get('type') == 'interrupt':
                    interrupted = True
                    break
                if message.get('type') == 'permission_decision' and bridge:
                    bridge.decide(message)
                elif message.get('type') == 'agent_response' and delegation:
                    delegation.respond(message)
                else:
                    problem = ('protocol', 'Unexpected native message during a Claude turn.')
                    break
                continue
            if message is None:
                if result is None:
                    problem = ('cli_exit', 'Claude exited without a final result.')
                break
            if message.get('type') == 'control_request':
                problem = ('permissions', 'Claude requested an unsupported permission transport; the action was stopped.')
                break
            if message.get('type') == 'control_response':
                response = message.get('response') if isinstance(message.get('response'), dict) else {}
                applied = (response.get('response') or {}).get('applied') if isinstance(response.get('response'), dict) else None
                if (response.get('request_id') == ULTRACODE_CHECK and response.get('subtype') == 'success'
                        and isinstance(applied, dict) and applied.get('ultracode') is False):
                    emit(dict(type='event', kind='notice', message=(
                        'Ultracode is not active for this Claude account (dynamic workflows are unavailable); '
                        'this turn runs at xhigh effort without workflows.')))
                continue
            if message.get('type') == 'system' and message.get('subtype') == 'background_tasks_changed':
                tasks = message.get('tasks') if isinstance(message.get('tasks'), list) else []
                # REPLACE semantics: the message lists every live background task.
                background = {task['task_id'] for task in tasks if isinstance(task, dict)
                              and isinstance(task.get('task_id'), str) and not task.get('ambient')
                              and task.get('task_type') in ('local_workflow', 'local_agent')}
                background_seen = background_seen or bool(background)
            if settle_deadline is not None and message.get('type') in ('assistant', 'stream_event', 'user'):
                settle_deadline = None  # A background report started the next turn.
            if message.get('type') == 'rate_limit_event':
                from manager_core.claude_usage import record_event
                try:
                    record_event(usage_store, hello['claude_profile_id'],
                                 record['settings'].get('account_identity'), message)
                except (OSError, ValueError, KeyError):
                    # A local usage-cache failure must not interrupt this answer.
                    pass
            if message.get('type') == 'system' and message.get('subtype') == 'init':
                actual = message.get('session_id')
                if actual and actual != record['id']:
                    problem = ('session_mismatch', 'Claude returned an unexpected session identifier.')
                    break
                if bridge or delegation:
                    servers = message.get('mcp_servers', [])
                    if not isinstance(servers, list):
                        servers = []
                    required = (['codex_bridge'] if bridge else []) + (['codex_agents'] if delegation else [])
                    if any(not any(s.get('name') == name and s.get('status') == 'connected'
                                   for s in servers if isinstance(s, dict)) for name in required):
                        problem = ('permissions', 'Claude could not connect its required native bridges.')
                        break
            if message.get('type') in ('assistant', 'stream_event'):
                model_started = True
            if message.get('type') == 'assistant':
                envelope = message.get('message')
                if isinstance(envelope, dict):
                    last_usage = usage_fields(envelope.get('usage')) or last_usage
            for event in normalize(message):
                if event['kind'] == 'tool_start' and isinstance(event.get('id'), str):
                    with bridge.guard if bridge else nullcontext():
                        observed[event['id']] = (event['tool'], event['input'])
                elif event['kind'] == 'tool_end':
                    with bridge.guard if bridge else nullcontext():
                        observed.pop(event.get('id'), None)
                elif event['kind'] == 'compact' and not event.get('summary_available'):
                    compactions += 1
                emit(dict(type='event', **event))
                if event['kind'] == 'rate_limit' and event.get('errorCode') == 'credits_required':
                    problem = ('credits_required', 'Claude included usage is exhausted; this turn has stopped.')
            if problem:
                break
            if message.get('type') == 'result':
                results.append(message)
                result = message
                if background:
                    if len(results) == 1:
                        emit(dict(type='event', kind='notice', message=(
                            'Claude is waiting for its background workflow; the answer continues when it reports back.')))
                    continue
                if not background_seen:
                    break
                # A report may already be queued; give the CLI a moment to start that turn.
                settle_deadline = time.monotonic() + BACKGROUND_SETTLE_SECONDS
    finally:
        stopped.set()
        try:
            process.stdin.close()
        except OSError:
            pass
        try:
            process.wait(timeout=5 if result is not None else 1)
        except subprocess.TimeoutExpired:
            if tree:
                tree.close()
            else:
                process.kill()
            process.wait(timeout=5)
        if tree:
            tree.close()
        process.stdout.close()
        process.stderr.close()
    if compactions:
        summary = compact_summary(configuration_directory, record['id'])
        if summary and summary != prior_summary:
            emit(dict(type='event', kind='compact', message='Claude supplied a portable compaction summary.',
                      summary=summary, summary_available=True))
    result = result or {}
    if len(results) > 1:
        # Each turn reports its own usage; the session cost stays cumulative.
        denials = [denial for item in results for denial in item.get('permission_denials') or []
                   if isinstance(item.get('permission_denials'), list)]
        result = dict(result, usage=sum_usage(*(item.get('usage') for item in results)), permission_denials=denials)
    success = bool(result) and not result.get('is_error') and not problem and not interrupted and process.returncode == 0
    checkpoint, checkpoint_failed = {}, False
    if success and compactions:
        emit(dict(type='event', kind='notice',
                  message='Generating a portable task checkpoint with one additional Claude response; tools are disabled.'))
        try:
            checkpoint = generate_checkpoint(command, cwd, environment, events, record['id'])
        except (ClaudeError, OSError, ProtocolError):
            checkpoint = {'success':False, 'cancelled':False, 'result':{}}
        compactions += checkpoint.get('compactions', 0)
        if checkpoint.get('limit_event'):
            emit(dict(type='event', **checkpoint['limit_event']))
        checkpoint_failed = not checkpoint['success']
        if checkpoint['cancelled']:
            interrupted, success = True, False
        if checkpoint['success']:
            coverage = {turn: value for turn, value in hello['snapshot'].get('turn_fingerprints', {}).items()
                        if turn != hello['turn_id']}
            emit(dict(type='event', kind='compact', summary=checkpoint['summary'], summary_available=True,
                      deliberate_full_context_summary=True, covered_turn_fingerprints=coverage,
                      message='Claude generated a portable summary covering the supplied task history.'))
            last_usage = checkpoint.get('last_usage') or last_usage
        else:
            emit(dict(type='event', kind='notice',
                      message='Portable checkpoint generation did not complete; the original task history is preserved.'))
    combined_usage = sum_usage(result.get('usage'), checkpoint.get('result', {}).get('usage'))
    total_cost = checkpoint.get('result', {}).get('total_cost_usd', result.get('total_cost_usd'))
    if not isinstance(total_cost, (int, float)) or isinstance(total_cost, bool) or not 0 <= total_cost < 1000000000:
        total_cost = None
    # Public headless documentation says --resume reports the conversation's
    # whole cumulative total. A lower/missing total is not a zero-cost turn.
    turn_cost = total_cost - prior_cost if total_cost is not None and total_cost >= prior_cost else None
    if checkpoint and 'total_cost_usd' not in checkpoint.get('result', {}):
        turn_cost = None  # A failed maintenance call may have consumed unreported usage.
    record['compactions'] = record.get('compactions', 0) + compactions
    ledger.save(record)
    done = dict(type='done', status='success' if success else 'interrupted' if interrupted else 'error',
                session_id=record['id'], result_text=bounded_text(result.get('result')) if success else '',
                usage=combined_usage, last_request_usage=last_usage,
                usage_complete=not checkpoint or bool(checkpoint.get('result', {}).get('usage')),
                checkpoint_usage=usage_fields(checkpoint.get('result', {}).get('usage')),
                cache={key: value for key, value in combined_usage.items() if key.startswith('cache_')},
                turn_cost_usd=turn_cost, permission_denials=result.get('permission_denials', []),
                duration_ms=int((time.monotonic() - start) * 1000), model_request_started=model_started)
    if not success and not interrupted:
        code, message = problem or ('claude_error', 'Claude did not complete this turn. Review the CLI account and permission status.')
        done['error'] = dict(code=code, message=message)
    emit(done)
    if success:
        # The model result is not durable task history yet. Rust records and
        # flushes it, then supplies complete output fingerprints in commit.
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                source, commit = events.get(timeout=min(1, deadline - time.monotonic()))
            except queue.Empty:
                continue
            if source != 'host':
                continue
            if commit is None or commit.get('type') != 'commit':
                raise ClaudeError('commit_missing', 'Claude result was not committed to canonical task history.')
            snapshot = commit.get('snapshot') or {}
            turn_ids = snapshot.get('turn_ids', [])
            fingerprints = snapshot.get('turn_fingerprints', {})
            expected = set(hello['snapshot']['turn_ids']) | {hello['turn_id']}
            if (not isinstance(turn_ids, list) or not all(isinstance(value, str) for value in turn_ids)
                    or not isinstance(fingerprints, dict) or not expected.issubset(turn_ids)
                    or any(not isinstance(fingerprints.get(turn), str) for turn in expected)):
                raise ClaudeError('commit_invalid', 'Claude result requires complete canonical turn fingerprints.')
            if any(fingerprints.get(turn) != value for turn, value in hello['snapshot'].get('turn_fingerprints', {}).items()
                   if turn != hello['turn_id']):
                raise ClaudeError('commit_invalid', 'Task history changed while Claude was running; the session must restart.')
            # Other accounts may have appended concurrent turns after hello.
            # Those were not seen by this Claude session and stay uncovered.
            record['covered'] = sorted(set(record.get('covered', [])) | expected)
            record['turn_fingerprints'] = {turn: fingerprints[turn] for turn in record['covered']}
            record['fingerprint'] = snapshot.get('fingerprint')
            record['dirty'] = checkpoint_failed
            record['cost_total_usd'] = total_cost if total_cost is not None else prior_cost
            ledger.save(record)
            emit(dict(type='committed', session_id=record['id']))
            return done
        raise ClaudeError('commit_missing', 'Claude result was not committed to canonical task history in time.')
    return done


def serve(root, profile_id, incoming=None, outgoing=None, cli_path=None, plugin_dirs=None,
          _test_command=None, _test_auth=None, _test_settings=None, execution_context=None):
    """Underscored injection points are only used by offline fake-CLI tests."""
    incoming = incoming or StdinLines(sys.stdin.fileno())
    outgoing = outgoing or sys.stdout.buffer
    guard = threading.Lock()
    def emit(value):
        payload = encode_message(value)
        with guard:
            outgoing.write(payload)
            outgoing.flush()
    root = Path(root).resolve()
    directory = (execution_context.ledger_directory if execution_context is not None
                 else root / 'work/control-center/claude')
    bridge, delegation, run_files = None, None, None
    try:
        profile_id = str(UUID(profile_id))
        if not directory.resolve().is_relative_to(root):
            raise ClaudeError('ledger_path', 'Claude session metadata must stay inside the manager repository.')
        if (directory / 'DISABLED').exists():
            raise ClaudeError('disabled', 'Claude execution is disabled in this manager.')
        hello = read_message(incoming)
        if hello is None:
            return 1
        cwd = validate_hello(hello, profile_id)
        system_prompt = system_prompt_text(hello.get('system_prompt', ''))
        remote = execution_context.prepare(cwd) if execution_context is not None else None
        if remote is not None:
            defaults = remote['settings']
        elif _test_settings is None:
            profile = Store(root).profile(profile_id)
            if profile.get('auth_mode') != 'claude_code':
                raise ClaudeError('profile_mismatch', 'This manager profile is not a Claude profile.')
            defaults = profile.get('claude_settings', {})
            cli_path = cli_path or defaults.get('cli_path')
        else:
            defaults = _test_settings
        settings = run_settings(dict(defaults, **{key: hello[key] for key in ('model', 'effort', 'context_window',
                                      'auto_compact_percent', 'autocompact_percent', 'exclude_dynamic_sections') if key in hello}))
        configuration_directory = (remote['configuration_directory'] if remote is not None
                                   else config_dir(profile_id))
        if remote is None and configuration_directory.is_relative_to(root):
            raise ClaudeError('configuration_path', 'Claude authentication must be stored outside the repository.')
        configuration_directory.mkdir(parents=True, exist_ok=True)
        environment = remote['environment'] if remote is not None else scrub_environment(configuration_directory)
        if remote is not None:
            cli, status = remote['cli'], remote['status']
        elif _test_auth is not None:
            cli = list(_test_command) if _test_command else [str(discover_cli(cli_path))]
            status = _test_auth
        else:
            from manager_core.claude_profiles import ClaudeProfiles
            profiles = ClaudeProfiles(Store(root))
            try:
                cli = [str(discover_cli(cli_path))]
                status = auth_status(profile_id, cli_path=cli[0])
            except ClaudeError as error:
                profiles.record_status(profile_id, dict(logged_in=False, status=error.code, message=str(error)))
                raise
            profiles.record_status(profile_id, status)
        if not status.get('logged_in'):
            raise ClaudeError('not_logged_in', 'Sign in through Claude CLI for this profile before starting a turn.')
        plugins = list(remote['plugins'] if remote is not None else (plugin_dirs or []))
        if not plugins and _test_command is None and remote is None:
            from manager_core.claude_skills import prepare_shared_skills
            plugin = prepare_shared_skills(root, profile_id, cwd)
            if plugin:
                plugins.append(plugin)
        settings['plugins'] = [str(Path(path).resolve()) for path in plugins]
        settings['instructions'] = instruction_fingerprint(cwd)
        settings['cli_version'] = status.get('cli_version', 'unknown')
        settings['account_identity'] = status.get('account_identity')
        delegation_tools = hello.get('delegation_tools', [])
        if not isinstance(delegation_tools, list):
            raise ProtocolError('Invalid native delegation tool catalog.')
        managed_delegation = hello.get('managed_delegation', False)
        if not isinstance(managed_delegation, bool):
            raise ProtocolError('Invalid native delegation policy.')
        managed_delegation = managed_delegation or bool(delegation_tools)
        if delegation_tools:
            settings['delegation_tools_hash'] = digest(delegation_tools)
        if managed_delegation:
            from manager_core.claude_delegation import GUIDANCE, LEAF_GUIDANCE
            effective_system_prompt = system_prompt_text(system_prompt + '\n' + (GUIDANCE if delegation_tools else LEAF_GUIDANCE))
        else:
            effective_system_prompt = system_prompt
        settings['system_prompt_hash'] = hashlib.sha256(effective_system_prompt.encode('utf-8')).hexdigest()
        ledger = SessionLedger(directory, profile_id, hello['thread_id'], settings, cwd)
        with session_lock(ledger.lock_path):
            previous = ledger.load(hello['snapshot']) if settings['account_identity'] else None
            emit(dict(type='ready', protocol=1, cli_version=status.get('cli_version', 'unknown'),
                      login={key: status[key] for key in ('logged_in', 'method') if key in status}, session=previous))
            request = read_message(incoming)
            if request is None or request.get('type') == 'interrupt':
                return 0
            if request.get('type') != 'run' or request.get('mode') not in ('fresh', 'resume'):
                raise ClaudeError('protocol', 'Expected a Claude run request.')
            if system_prompt_text(request.get('system_prompt', system_prompt)) != system_prompt:
                raise ClaudeError('settings_changed', 'Effective project instructions changed after the session handshake.')
            requested_settings = run_settings(dict(settings, **{key: request[key] for key in ('model', 'effort',
                                               'context_window', 'auto_compact_percent', 'exclude_dynamic_sections') if key in request}))
            if any(requested_settings[key] != settings[key] for key in requested_settings):
                raise ClaudeError('settings_changed', 'Claude settings changed after the session handshake; restart the turn.')
            resume = request['mode'] == 'resume'
            if resume and previous is None:
                raise ClaudeError('session_changed', 'Claude needs a fresh session with the complete task history.')
            if resume and any(block.get('kind') == 'context' for block in request.get('blocks', []) if isinstance(block, dict)):
                raise ClaudeError('duplicate_context', 'A resumed Claude session accepts only unseen history updates.')
            hello['_prompt'] = prompt_message(request.get('blocks'))
            record = dict(previous) if resume else dict(id=str(uuid4()), covered=[], fingerprint=None,
                         turn_fingerprints={}, model=settings['model'], effort=settings['effort'],
                         dirty=False, compactions=0, cost_total_usd=0, settings=settings)
            record.update(model=settings['model'], effort=settings['effort'], settings=settings)
            mode, prompts = request.get('permission_mode', 'plan'), request.get('permission_prompts', 'none')
            stopped, observed = threading.Event(), {}
            mcp_path = None
            servers = {}
            system_prompt_path = None
            if effective_system_prompt:
                run_files = private_temporary_directory('codex-claude-instructions-')
                system_prompt_path = Path(run_files.name) / 'instructions.txt'
                system_prompt_path.write_text(effective_system_prompt, encoding='utf-8')
            if prompts == 'host':
                bridge = PermissionBridge(emit, stopped, mode, observed)
                mcp_path = Path(bridge.temp.name) / 'mcp.json'
                servers.update(bridge.configuration()['mcpServers'])
            if delegation_tools:
                from manager_core.claude_delegation import DelegationBridge
                delegation = DelegationBridge(emit, stopped, delegation_tools, private_temporary_directory)
                mcp_path = mcp_path or Path(delegation.temp.name) / 'mcp.json'
                servers.update(delegation.configuration())
            if mcp_path is not None:
                atomic_json(mcp_path, {'mcpServers': servers})
            command = build_command(cli, record['id'], resume, settings, mode, prompts,
                                    configuration_directory, root, plugins, mcp_path, system_prompt_path,
                                    managed_delegation=managed_delegation)
            execute(command, cwd, environment, incoming, emit, bridge, stopped, observed,
                    record, ledger, hello, configuration_directory,
                    None if remote is not None else Store(root), delegation)
        return 0
    except (ClaudeError, ProtocolError, OSError, ValueError, KeyError) as error:
        code = error.code if isinstance(error, ClaudeError) else 'runner_error'
        message = str(error) if isinstance(error, (ClaudeError, ProtocolError)) else 'Claude runner could not complete this operation.'
        emit(dict(type='refused', code=code, message=message))
        return 1
    finally:
        if delegation:
            delegation.close()
        if bridge:
            bridge.close()
        if run_files:
            run_files.cleanup()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['serve'])
    parser.add_argument('--root', required=True)
    parser.add_argument('--profile', required=True)
    parser.add_argument('--cli')
    parser.add_argument('--plugin-dir', action='append')
    args = parser.parse_args(argv)
    return serve(args.root, args.profile, cli_path=args.cli, plugin_dirs=args.plugin_dir)


if __name__ == '__main__':
    raise SystemExit(main())
