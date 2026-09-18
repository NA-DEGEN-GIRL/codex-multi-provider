"""Instance-scoped navigation and a payload-free passive runtime observer.

An argv deep link is a request, never evidence that a conversation is selected.
The installed 26.903.9818.0 parser ignores a thread URL's hostId query; remote
navigation therefore fails closed. Foreign local records need an exact read-only
projection or a registered managed source with current durable owner authority.
No global URL protocol handler is used.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sqlite3
import threading
import time
from typing import Any
from uuid import UUID, uuid4

from . import authority


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def identifier(value: Any) -> str | None:
    """Retain identifiers only; arbitrary provider output is never a log field."""
    if isinstance(value, str) and 0 < len(value) <= 160:
        if all(c.isascii() and (c.isalnum() or c in '_-:.') for c in value):
            return value
    return None


def request_key(value: Any) -> str | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return 'number:' + str(value)
    if isinstance(value, str) and len(value) <= 512:
        # Caller-controlled request IDs can contain private data. Keep only a digest.
        return 'string:' + hashlib.sha256(value.encode()).hexdigest()
    return None


def identity_fingerprint(email: str) -> str:
    return hashlib.sha256(('codex-manager-email-v1\0' + email.strip().casefold()).encode()).hexdigest()


class AppTransport:
    def __init__(self, root: Path | str, *, popen=None):
        self.root = Path(root).resolve()
        self._popen = popen or subprocess.Popen

    def _profile(self, profile: dict) -> tuple[str, Path, Path]:
        profile_id = str(UUID(str(profile['id'])))
        directory = self.root / 'work/control-center/profiles' / profile_id
        home, ui_home = directory / 'codex', directory / 'ui'
        for actual, expected in ((profile.get('home'), home), (profile.get('ui_home'), ui_home)):
            if actual is None or expected.resolve() != expected or Path(actual).resolve() != expected:
                raise ValueError('Instance paths do not match this managed profile.')
        return profile_id, home, ui_home

    def open_conversation(self, profile: dict, shortcut: dict, app_executable: str,
                          environment: dict | None = None, *, read_only: bool = False) -> dict:
        profile_id, home, ui_home = self._profile(profile)
        thread_id = str(UUID(str(shortcut['thread_id'])))
        request_id = str(uuid4())
        result = {'request_id': request_id, 'profile_id': profile_id,
                  'thread_id': thread_id, 'selection_verified': False}
        if shortcut.get('profile_id') != profile_id:
            return {**result, 'state': 'blocked', 'reason': 'profile_mismatch'}
        if shortcut.get('host_id', 'local') != 'local':
            return {**result, 'state': 'blocked', 'reason': 'exact_remote_navigation_unverified',
                    'message': '원본 앱의 링크가 SSH 호스트를 지정하지 못해 정확한 연결을 확인할 수 없습니다.'}
        native_thread_id = thread_id
        canonical_home = (environment or {}).get('CODEX_RECORD_HOME')
        shared_execution = (environment or {}).get('CODEX_MANAGER_SHARED_EXECUTION') == '1' and not read_only
        if shared_execution and canonical_home:
            try:
                database = Path(canonical_home).resolve() / 'state_5.sqlite'
                with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=2)) as db:
                    present = db.execute('SELECT 1 FROM threads WHERE id=?', (thread_id,)).fetchone()
            except (OSError, ValueError, sqlite3.Error):
                present = None
            if not present:
                return {**result, 'state': 'blocked', 'reason': 'canonical_task_missing',
                        'message': '공통 저장소에서 이 작업을 찾지 못했습니다. 작업 목록을 새로 고쳐 주세요.'}
            result.update(readonly_projection=False, shared_execution=True)
        elif shared_execution:
            if shortcut.get('source_store_id') != 'manager:' + profile_id and self._projection(profile, shortcut, environment) is None:
                return {**result, 'state': 'blocked', 'reason': 'unregistered_source'}
            result.update(readonly_projection=False, shared_execution=True)
        elif read_only or shortcut.get('source_store_id') != 'manager:' + profile_id:
            if not read_only and self._managed_source(profile, shortcut, environment):
                result['readonly_projection'] = False
            else:
                native_thread_id = self._projection(profile, shortcut, environment)
                if native_thread_id is None:
                    return {**result, 'state': 'blocked', 'reason': 'foreign_store_binding_required',
                            'message': '다른 저장소의 작업은 실행 권한과 원본 기록 연결을 먼저 확인해야 합니다.'}
                result['readonly_projection'] = True
                result['projection_thread_id'] = native_thread_id
        if type(profile.get('process_id')) is not int or profile['process_id'] <= 0:
            return {**result, 'state': 'blocked', 'reason': 'profile_not_running'}
        if canonical_home or (environment or {}).get('CODEX_MANAGER_SHARED_CATALOG'):
            readiness = self.navigation_readiness(profile, require_execution=shared_execution)
            if readiness is not None:
                return {**result, **readiness}
        executable = Path(app_executable)
        if not executable.is_absolute() or not executable.is_file() or executable.name.lower() != 'chatgpt.exe':
            raise ValueError('Expected an installed absolute ChatGPT.exe path.')
        # Preserve the supplied launch environment (including provider keys in memory),
        # but overwrite isolation identity and never serialize environment values.
        env = dict(environment if environment is not None else os.environ)
        env['CODEX_HOME'] = str(home)
        env['CODEX_ELECTRON_USER_DATA_PATH'] = str(ui_home)
        from .desktop_bundle import pipe_name
        env['CODEX_MANAGER_DESKTOP_PIPE'] = pipe_name(profile_id)
        env.pop('ELECTRON_RUN_AS_NODE', None)
        uri = 'codex://threads/' + native_thread_id
        process = self._popen([str(executable), '--user-data-dir=' + str(ui_home), uri],
                              env=env, cwd=str(self.root), stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        return {**result, 'state': 'request_sent', 'uri': uri,
                'launcher_process_id': process.pid, 'requested_at': utc_now(),
                'message': '지정 프로필에 작업 열기를 요청했습니다. 화면 선택 확인은 아직 대기 중입니다.'}

    def navigation_readiness(self, profile, *, require_execution=False):
        observed = self.observe(profile)
        if observed.get('initialized') is not True or observed.get('connected') is not True:
            return dict(state='waiting_for_reader', reason='runtime_starting',
                        message='Codex를 준비하고 있습니다. 준비되면 선택한 대화로 자동 이동합니다.')
        if (observed.get('shared_catalog', {}).get('enabled') is not True
                and observed.get('canonical_storage', {}).get('enabled') is not True):
            return dict(state='blocked', reason='shared_catalog_runtime_unavailable',
                        message='이 실행에는 공통 대화 목록 기능이 적용되지 않았습니다. 이 관리용 Codex를 다음에 다시 열 때 적용됩니다.')
        if require_execution and observed.get('shared_execution_version', 0) < 1:
            return dict(state='blocked', reason='shared_execution_pending',
                        message='이 창은 이전 조회용 실행입니다. 관리용 Codex를 다시 열면 선택한 계정으로 작업할 수 있습니다.')
        return None

    @staticmethod
    def _safe_object(path: Path, limit: int) -> dict:
        info = path.lstat()
        if (path.is_symlink() or getattr(info, 'st_file_attributes', 0) & 0x400
                or info.st_size > limit or info.st_nlink != 1):
            raise ValueError('Managed navigation record is not a bounded private file.')
        value = json.loads(path.read_text(encoding='utf-8-sig'))
        if not isinstance(value, dict):
            raise ValueError('Managed navigation record must be an object.')
        return value

    def _managed_source(self, profile: dict, shortcut: dict, environment: dict | None) -> bool:
        """Authorize canonical navigation only from current durable owner evidence.

        A shortcut flag, successful prior resume, or advisory idle observation is
        insufficient. The runtime independently enforces the same authority when
        it receives the link; this check does not grant or transfer write access.
        """
        try:
            profile_id = profile['id']
            source_id = shortcut.get('source_store_id')
            if not isinstance(source_id, str) or not source_id.startswith('manager:'):
                return False
            source_profile_id = str(UUID(source_id[8:]))
            if source_id != 'manager:' + source_profile_id:
                return False
            directory = self.root / 'work/control-center'
            owner_directory = directory / 'profiles' / profile_id
            manifest_path = owner_directory / 'managed-sources.json'
            supplied = (environment or {}).get('CODEX_MANAGER_MANAGED_SOURCES')
            if (not supplied or not Path(supplied).is_absolute()
                    or Path(supplied) != manifest_path
                    or owner_directory.resolve(strict=True) != owner_directory
                    or Path(supplied).resolve(strict=True) != manifest_path):
                return False
            manifest = self._safe_object(manifest_path, 4 * 1024 * 1024)
            if (set(manifest) != {'version', 'hostId', 'profileId', 'sources', 'bindings'}
                    or type(manifest['version']) is not int or manifest['version'] != 1
                    or manifest['hostId'] != 'local' or manifest['profileId'] != profile_id
                    or not isinstance(manifest['sources'], list) or len(manifest['sources']) > 256
                    or not isinstance(manifest['bindings'], list) or len(manifest['bindings']) > 4096):
                return False
            # Require unique identities, not the first matching row in a possibly
            # stale or collision-bearing catalog.
            entries = [s for s in manifest['sources'] if isinstance(s, dict)
                       and s.get('hostId') == 'local' and s.get('sourceStoreId') == source_id]
            bindings = [b for b in manifest['bindings'] if isinstance(b, dict)
                        and b.get('threadId') == shortcut['thread_id']]
            if len(entries) != 1 or len(bindings) != 1:
                return False
            entry, binding = entries[0], bindings[0]
            if (set(entry) != {'hostId', 'sourceStoreId', 'codexHome'}
                    or set(binding) != {'threadId', 'hostId', 'sourceStoreId', 'ownerProfileId', 'ownershipEpoch', 'recordRevision'}
                    or binding['hostId'] != 'local' or binding['sourceStoreId'] != source_id
                    or binding['ownerProfileId'] != profile_id):
                return False
            home = directory / 'profiles' / source_profile_id / 'codex'
            if (home.resolve(strict=True) != home or not home.is_dir()
                    or not Path(entry['codexHome']).is_absolute()
                    or Path(entry['codexHome']) != home
                    or Path(entry['codexHome']).resolve(strict=True) != home):
                return False
            state = self._safe_object(directory / 'state.json', 16 * 1024 * 1024)
            if state.get('version') != 1:
                return False
            source_profiles = [p for p in state.get('profiles', []) if isinstance(p, dict)
                               and p.get('id') == source_profile_id]
            target_profiles = [p for p in state.get('profiles', []) if isinstance(p, dict)
                               and p.get('id') == profile_id]
            registered_sources = [s for s in state.get('sources', []) if isinstance(s, dict)
                                  and s.get('id') == source_id and s.get('host_id') == 'local']
            if len(source_profiles) != 1 or len(target_profiles) != 1 or len(registered_sources) != 1:
                return False
            if (source_profiles[0].get('view_only') or
                    target_profiles[0].get('view_only') or target_profiles[0].get('account_missing') or
                    Path(target_profiles[0]['home']) != owner_directory / 'codex' or
                    Path(target_profiles[0]['ui_home']) != owner_directory / 'ui' or
                    Path(source_profiles[0]['home']) != home or
                    Path(registered_sources[0]['home']) != home or
                    Path(source_profiles[0]['home']).resolve(strict=True) != home or
                    Path(registered_sources[0]['home']).resolve(strict=True) != home):
                return False
            marker = self._safe_object(home / 'managed-source.json', 4096)
            if marker != {'host_id': 'local', 'store_id': source_id}:
                return False
            self._safe_object(home / 'managed-authority' / (shortcut['thread_id'] + '.json'), 4096)
            grant = authority.read(home, shortcut['thread_id'])
            expected = {'version': 1, 'host_id': 'local', 'store_id': source_id,
                        'thread_id': shortcut['thread_id'], 'owner_profile_id': profile_id,
                        'epoch': binding['ownershipEpoch'], 'revision': binding['recordRevision']}
            authority.validate(expected)  # Reject bool counters and noncanonical IDs.
            return grant == expected
        except (OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError):
            return False

    def _projection(self, profile: dict, shortcut: dict, environment: dict | None) -> str | None:
        path_value = ((environment or {}).get('CODEX_MANAGER_RECORD_CATALOG')
                      or (environment or {}).get('CODEX_MANAGER_SHARED_CATALOG') or profile.get('record_catalog_path'))
        if not path_value:
            return None
        try:
            path = Path(path_value).resolve()
            path.relative_to((self.root / 'work/control-center').resolve())
            if path.stat().st_size > 8_000_000:
                return None
            catalog = json.loads(path.read_text(encoding='utf-8'))
            if catalog.get('version') == 3:
                from .source_catalog import project
                from .store import Store
                return project(self.root, Path(path_value), Store(self.root).read()['sources'], shortcut)
            if catalog.get('version') != 1 or catalog.get('hostId') != 'local':
                return None
            matches = [entry for entry in catalog.get('entries', []) if isinstance(entry, dict)
                       and entry.get('threadId') == shortcut['thread_id']
                       and entry.get('hostId') == shortcut.get('host_id', 'local')
                       and entry.get('sourceStoreId') == shortcut.get('source_store_id')]
            if len(matches) != 1:
                return None
            projection = str(UUID(matches[0]['projectionThreadId']))
            if projection == shortcut['thread_id']:
                return None
            if sum(1 for entry in catalog['entries'] if entry.get('projectionThreadId') == projection) != 1:
                return None
            return projection
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return None

    def observe(self, profile: dict) -> dict:
        profile_id, _, _ = self._profile(profile)
        path = self.root / 'work/control-center/instances' / profile_id / 'runtime-state.json'
        unknown = {'profile_id': profile_id, 'activity': 'unknown',
                   'selection_verified': False, 'safe_to_restart': False,
                   'source': 'passive_runtime_observer'}
        try:
            if path.stat().st_size > 2_000_000:
                return {**unknown, 'reason': 'observer_snapshot_too_large'}
            snapshot = json.loads(path.read_text(encoding='utf-8'))
            if snapshot.get('profile_id') != profile_id or snapshot.get('schema_version') != 1:
                return {**unknown, 'reason': 'observer_identity_mismatch'}
            if not profile.get('generation') or snapshot.get('generation') != profile['generation']:
                return {**unknown, 'reason': 'observer_generation_mismatch'}
            age = time.time() - datetime.fromisoformat(snapshot['observed_at']).timestamp()
            if age < -5 or age > 15:
                return {**unknown, 'reason': 'observer_stale'}
            # A passive channel does not prove all detached tools, queues or remote
            # children have quiesced. It must never authorize automatic shutdown.
            snapshot['safe_to_restart'] = False
            snapshot['selection_verified'] = False
            return snapshot
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return {**unknown, 'reason': 'observer_unavailable'}


class RuntimeObserver:
    """Reduce mirrored app-server RPCs without persisting prompts or credentials.

    Activity is positive evidence of work. Absence of observed work is not proof
    of global idle: queue state, detached processes and other transports may exist.
    """
    _MUTATING = ('turn/', 'thread/queue/', 'thread/realtime/', 'review/',
                 'process/', 'command/', 'mcpServer/tool/', 'thread/shellCommand')

    def __init__(self, profile_id: str, *, runtime_pid: int | None = None, read_only_projection=None):
        self.profile_id = str(UUID(profile_id))
        self.runtime_pid = runtime_pid
        self.read_only_projection = read_only_projection or (lambda _: False)
        self.lock = threading.RLock()
        self.initialized = False
        self.initialize_succeeded = False
        self.connected = True
        self.integrity = True
        self.integrity_reasons: set[str] = set()
        self.pending: dict[str, tuple[str, str | None]] = {}
        self.server_pending: set[str] = set()
        self.active_turns: set[tuple[str, str]] = set()
        self.active_threads: set[str] = set()
        self.active_tools: set[tuple[str, str]] = set()
        self.active_children: set[str] = set()
        self.active_processes: set[str] = set()
        self.queue_unknown: set[str] = set()
        self.account: dict = {'state': 'unknown'}
        self.last_thread_read: str | None = None
        self.opened_task: dict = {}
        self.messages = 0
        self.diagnostics: list[dict] = []
        self.diagnostic_sequence = 0

    def _diagnostic(self, method, state, error=None, result=None):
        # Never retain request/response bodies, arbitrary methods or raw errors.
        if method not in ('initialize', 'configRequirements/read', 'account/read',
                          'thread/list', 'thread/read', 'thread/resume', 'thread/turns/list',
                          'thread/items/list', 'thread/timeline/list', 'turn/start',
                          'project/list', 'project/import', 'thread/name/set', 'thread/settings/update',
                          'turn/settings/update', 'config/value/write', 'config/batchWrite', 'windowsSandbox/readiness',
                          'windowsSandbox/setupStart', 'windowsSandbox/setupCompleted'):
            return
        self.diagnostic_sequence += 1
        event = dict(sequence=self.diagnostic_sequence, at=utc_now(), method=method, state=state)
        if isinstance(error, dict):
            if type(error.get('code')) is int:
                event['code'] = error['code']
            if error.get('message') == 'record catalog or history query changed; restart listing':
                event['reason'] = 'history_cursor_changed'
            elif error.get('message') == 'record catalog or query changed; restart listing':
                event['reason'] = 'catalog_cursor_changed'
            elif error.get('message') == 'project, section, and ancestry queries require an authoritative catalog adapter':
                event['reason'] = 'catalog_filter_unsupported'
            elif isinstance(error.get('message'), str) and error['message'].startswith('This instance is a read-only record catalog.'):
                event['reason'] = 'catalog_record_readonly'
        if isinstance(result, dict):
            if method == 'windowsSandbox/readiness' and result.get('status') in ('ready', 'notConfigured', 'updateRequired'):
                event['reason'] = 'windows_sandbox_' + result['status']
            elif method == 'windowsSandbox/setupStart' and type(result.get('started')) is bool:
                event['reason'] = 'windows_setup_started' if result['started'] else 'windows_setup_not_started'
            elif method == 'windowsSandbox/setupCompleted' and type(result.get('success')) is bool:
                event['reason'] = 'windows_setup_succeeded' if result['success'] else 'windows_setup_failed'
        if method == 'windowsSandbox/setupStart' and state == 'failed':
            event['reason'] = 'windows_setup_request_failed'
        self.diagnostics.append(event)
        self.diagnostics = self.diagnostics[-60:]

    def consume(self, direction: str, message: Any) -> None:
        with self.lock:
            if direction not in ('client', 'server') or not isinstance(message, dict):
                self._taint('invalid_message')
                return
            self.messages += 1
            method = message.get('method')
            params = message.get('params')
            params = params if isinstance(params, dict) else {}
            key = request_key(message.get('id'))
            if direction == 'client':
                if method == 'initialized':
                    self.initialized = self.initialize_succeeded
                    if not self.initialize_succeeded:
                        self._taint('initialize_order')
                if method == 'initialize':
                    capabilities = params.get('capabilities')
                    if isinstance(capabilities, dict) and capabilities.get('optOutNotificationMethods'):
                        self._taint('notification_opt_out')
                if method == 'process/spawn':
                    handle = identifier(params.get('processHandle'))
                    if handle:
                        self.active_processes.add(handle)
                if isinstance(method, str) and key is not None:
                    if len(self.pending) >= 4096:
                        self._taint('client_request_limit')
                    else:
                        # Only known method names and UUID-ish identifiers survive.
                        safe_method = method if len(method) <= 100 and all(c.isascii() and (c.isalnum() or c in '/_-') for c in method) else 'unknown'
                        self.pending[key] = (safe_method, identifier(params.get('threadId')))
                        self._diagnostic(safe_method, 'started')
                elif key is not None:
                    self.server_pending.discard(key)
                return
            if key is not None and isinstance(method, str):
                if len(self.server_pending) < 4096:
                    self.server_pending.add(key)
                else:
                    self._taint('server_request_limit')
                return
            if key is not None:
                request = self.pending.pop(key, None)
                if request is not None:
                    self._diagnostic(request[0], 'failed' if 'error' in message else 'completed', message.get('error'), message.get('result'))
                if request is not None and 'error' not in message:
                    self._response(request, message.get('result'))
                return
            if method == 'windowsSandbox/setupCompleted':
                self._diagnostic(method, 'completed' if params.get('success') is True else 'failed', result=params)
            thread_id = identifier(params.get('threadId'))
            if method == 'thread/name/updated' and self.opened_task.get('thread_id') == thread_id:
                name = params.get('threadName')
                if isinstance(name, str):
                    self.opened_task['title'] = ' '.join(name.split())[:512]
            if thread_id and self.read_only_projection(thread_id):
                if method == 'thread/deleted' and hasattr(self.read_only_projection, 'forget'):
                    try:
                        self.read_only_projection.forget(thread_id)
                    except (OSError, ValueError, KeyError, TypeError, AttributeError):
                        self._taint('catalog_membership_unavailable')
                return
            if method in ('turn/started', 'turn/completed'):
                turn = params.get('turn')
                turn_id = identifier(turn.get('id')) if isinstance(turn, dict) else None
                if thread_id and turn_id:
                    pair = (thread_id, turn_id)
                    if method == 'turn/started':
                        self.active_turns.add(pair)
                    elif isinstance(turn, dict) and turn.get('status') in ('completed', 'interrupted', 'failed'):
                        self.active_turns.discard(pair)
                    else:
                        self._taint('unknown_turn_completion')
            elif method == 'thread/status/changed' and thread_id:
                self._thread_status(thread_id, params.get('status'))
            elif method == 'thread/started':
                self._thread(params.get('thread'))
            elif method == 'thread/queue/changed' and thread_id:
                self.queue_unknown.add(thread_id)
            elif method in ('item/started', 'item/completed'):
                self._item(method, thread_id, params.get('item'))
            elif method == 'process/exited':
                handle = identifier(params.get('processHandle'))
                if handle:
                    self.active_processes.discard(handle)
            elif method == 'serverRequest/resolved':
                resolved = request_key(params.get('requestId'))
                if resolved:
                    self.server_pending.discard(resolved)
            elif method == 'account/updated':
                # authMode does not identify a person. Invalidate previous identity.
                self.account = {'state': 'unknown', 'reason': 'account_changed'}

    def _response(self, request: tuple[str, str | None], result: Any) -> None:
        method, requested_thread = request
        if not isinstance(result, dict):
            return
        if method == 'initialize':
            self.initialize_succeeded = True
            # App-server commits the connection while handling initialize.
            # Native desktop clients need not send an initialized notification.
            self.initialized = True
        if method == 'account/read':
            account = result.get('account')
            if account is None:
                self.account = {'state': 'signed_out'}
            elif isinstance(account, dict) and account.get('type') == 'chatgpt':
                email = account.get('email')
                self.account = {'state': 'signed_in', 'type': 'chatgpt', 'identity_scope': 'email_only'}
                if isinstance(email, str) and len(email) <= 512:
                    self.account['email_fingerprint'] = identity_fingerprint(email)
                # Email alone cannot distinguish multiple workspaces under one login.
                self.account['workspace_verified'] = False
            elif isinstance(account, dict) and account.get('type') in ('apiKey', 'amazonBedrock'):
                self.account = {'state': 'signed_in', 'type': account['type']}
        if method in ('thread/start', 'thread/resume', 'thread/read', 'thread/fork'):
            self._thread(result.get('thread'))
            thread = result.get('thread')
            if method != 'thread/read' and isinstance(thread, dict) and identifier(thread.get('id')):
                name = thread.get('name') or thread.get('title') or ''
                self.opened_task = dict(thread_id=thread['id'], title=' '.join(str(name).split())[:512],
                                        source='last_successful_open', observed_at=utc_now())
            if method == 'thread/read':
                thread = result.get('thread')
                self.last_thread_read = identifier(thread.get('id')) if isinstance(thread, dict) else requested_thread
        elif method == 'thread/list':
            data = result.get('data')
            if isinstance(data, list):
                for thread in data[:10000]:
                    self._thread(thread)

    def _thread(self, thread: Any) -> None:
        if not isinstance(thread, dict):
            return
        observe_origin = getattr(self.read_only_projection, 'observe_thread', None)
        if observe_origin:
            try:
                observe_origin(thread)
            except (ValueError, KeyError, TypeError, AttributeError):
                self._taint('invalid_catalog_origin')
        thread_id = identifier(thread.get('id'))
        if thread_id and self.read_only_projection(thread_id):
            return
        if thread_id:
            self._thread_status(thread_id, thread.get('status'))
            for turn in thread.get('turns', []) if isinstance(thread.get('turns'), list) else []:
                turn_id = identifier(turn.get('id')) if isinstance(turn, dict) else None
                if turn_id and turn.get('status') == 'inProgress':
                    self.active_turns.add((thread_id, turn_id))

    def _thread_status(self, thread_id: str, status: Any) -> None:
        kind = status.get('type') if isinstance(status, dict) else None
        if kind == 'active':
            self.active_threads.add(thread_id)
        elif kind in ('idle', 'notLoaded', 'systemError'):
            self.active_threads.discard(thread_id)

    def _item(self, method: str, thread_id: str | None, item: Any) -> None:
        if not isinstance(item, dict):
            self._taint('invalid_item_event')
            return
        kind, item_id = item.get('type'), identifier(item.get('id'))
        if thread_id and item_id and kind in ('commandExecution', 'mcpToolCall', 'dynamicToolCall',
                                               'fileChange', 'collabAgentToolCall'):
            pair = (thread_id, item_id)
            if method == 'item/started':
                self.active_tools.add(pair)
            else:
                self.active_tools.discard(pair)
        if kind == 'collabAgentToolCall':
            states = item.get('agentsStates')
            if isinstance(states, dict):
                for child, state in states.items():
                    child_id = identifier(child)
                    if child_id and isinstance(state, dict):
                        status = state.get('status')
                        if status in ('pendingInit', 'running'):
                            self.active_children.add(child_id)
                        elif status in ('interrupted', 'completed', 'errored', 'shutdown', 'notFound'):
                            self.active_children.discard(child_id)
        elif kind == 'subAgentActivity':
            child_id = identifier(item.get('agentThreadId'))
            if child_id:
                if item.get('kind') in ('started', 'interacted'):
                    self.active_children.add(child_id)
                elif item.get('kind') in ('interrupted', 'completed'):
                    self.active_children.discard(child_id)

    def _taint(self, reason) -> None:
        self.integrity = False
        self.integrity_reasons.add(reason)

    def gap(self) -> None:
        with self.lock:
            self._taint('protocol_gap')

    def disconnected(self) -> None:
        with self.lock:
            self.connected = False

    def snapshot(self) -> dict:
        with self.lock:
            mutating = sum(1 for method, _ in self.pending.values() if method.startswith(self._MUTATING))
            active = bool(self.active_turns or self.active_threads or self.server_pending or mutating
                          or self.active_children or self.active_processes or self.active_tools)
            return {'schema_version': 1, 'profile_id': self.profile_id,
                    'runtime_process_id': self.runtime_pid, 'observed_at': utc_now(),
                    'source': 'passive_runtime_observer', 'connected': self.connected,
                    'initialized': self.initialized, 'stream_complete': self.integrity,
                    'stream_incomplete_reasons': sorted(self.integrity_reasons),
                    'activity': 'active' if active and self.connected else 'unknown',
                    'no_activity_observed': not active,
                    'active_turn_count': len(self.active_turns),
                    'active_tool_count': len(self.active_tools),
                    'active_child_count': len(self.active_children),
                    'active_process_count': len(self.active_processes),
                    'active_thread_ids': sorted(self.active_threads),
                    'recent_diagnostics': list(self.diagnostics),
                    'pending_client_request_count': len(self.pending),
                    'pending_execution_request_count': mutating,
                    'pending_server_request_count': len(self.server_pending),
                    'queue_state_unknown_count': len(self.queue_unknown),
                    'observed_message_count': self.messages,
                    'account': dict(self.account), 'last_thread_read': self.last_thread_read,
                    'opened_task': dict(self.opened_task),
                    'selection_verified': False, 'safe_to_restart': False,
                    'limitations': ['selection_not_observable_from_runtime_rpc',
                                    'detached_tools_and_remote_transports_not_fully_observed']}

    def write_snapshot(self, path: Path | str) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + '.tmp-' + str(os.getpid()))
        temporary.write_text(json.dumps(self.snapshot(), ensure_ascii=False), encoding='utf-8')
        os.replace(temporary, target)


class JsonLineObserver:
    """Bounded decoder. Oversized/malformed messages pass through but taint status."""
    def __init__(self, observer: RuntimeObserver, direction: str, limit: int = 16 * 1024 * 1024):
        self.observer, self.direction, self.limit = observer, direction, limit
        self.pending = bytearray()
        self.dropping = False

    def feed(self, chunk: bytes) -> None:
        for part in chunk.splitlines(keepends=True):
            complete = part.endswith(b'\n')
            if not self.dropping:
                if len(self.pending) + len(part) > self.limit:
                    self.observer.gap()
                    self.pending.clear()
                    self.dropping = True
                else:
                    self.pending.extend(part)
            if complete:
                if not self.dropping and self.pending.strip():
                    try:
                        self.observer.consume(self.direction, json.loads(self.pending))
                    except (ValueError, UnicodeError, RecursionError):
                        self.observer.gap()
                self.pending.clear()
                self.dropping = False

    def finish(self) -> None:
        if self.pending or self.dropping:
            self.observer.gap()
