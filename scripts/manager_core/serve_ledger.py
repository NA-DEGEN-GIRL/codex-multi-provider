"""Per-profile serve ledger and per-provider thread settings, fed by the runtime proxy.

The RuntimeProxy already mirrors every app-server message. This module keeps a
small, content-free record of each upstream request a profile served: token
counts, provider, model, effort, tier and a salted hash of the thread id. It
never stores prompts, bodies, titles, paths or credentials.

The runtime also emits token-count events that are not requests: a restored
snapshot after resume/fork, a recomputed estimate after a provider switch
(``last.inputTokens == 0``) or a compaction, and rate-limit re-emissions of an
unchanged total. A row is written only when the thread's cumulative total
advanced past a baseline this proxy has already seen, and ``last`` reports a
real input count.

The same observations give the last (model, effort, tier) each provider used
for a thread. The desktop resume adapter marks an open that returns a thread to
another provider; ``ProviderReturn`` then restores that provider's last effort
and tier instead of the profile default, so the return does not break the
provider's cached prefix with an effort change.
"""
from __future__ import annotations

from collections import OrderedDict
import copy
import hashlib
import json
import os
from pathlib import Path
import queue
import threading
import time
from typing import Any, Callable

LEDGER_FILE = 'serve-ledger.jsonl'
LEDGER_OLD_FILE = 'serve-ledger.old.jsonl'
SETTINGS_FILE = 'thread-settings.json'
LEDGER_VERSION = 1
MAX_LEDGER_BYTES = 2 * 1024 * 1024
MAX_SETTINGS_BYTES = 1024 * 1024
MAX_SETTINGS_THREADS = 2000
MAX_TRACKED_THREADS = 1024
MAX_PENDING = 512
SETTINGS_WRITE_INTERVAL = 2.0
# Marker the desktop resume adapter adds to thread/resume|fork when the thread
# was last served by another provider. The proxy strips it before forwarding.
PROVIDER_RETURN_MARKER = 'codexManagerProviderReturn'
OPENAI_FAMILY = 'openai'
# 'ultracode' is a Claude Code profile choice; a restore applies only to the
# same provider and model that recorded it.
EFFORTS = frozenset(('none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra', 'ultracode'))
_TRACKED_REQUESTS = frozenset(('thread/start', 'thread/resume', 'thread/fork',
                               'thread/settings/update', 'turn/settings/update'))
_STOP = object()


def thread_hash(thread_id: str) -> str:
    """Domain-separated thread digest shared with the Shell (ProfileCacheLine.ThreadHash)."""
    value = ('codex-manager-thread-v1\0' + str(thread_id).strip().lower()).encode('utf-8')
    return hashlib.sha256(value).hexdigest()[:24]


def _turn_hash(turn_id: str) -> str:
    return hashlib.sha256(('codex-manager-turn-v1\0' + turn_id).encode('utf-8')).hexdigest()[:12]


def _token(value: Any, limit: int = 160) -> str | None:
    """Identifiers only (model slugs, provider ids, efforts); never free text."""
    if isinstance(value, str) and 0 < len(value) <= limit:
        if all(c.isascii() and (c.isalnum() or c in '_-:.') for c in value):
            return value
    return None


def _request_key(value: Any) -> str | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return 'number:' + str(value)
    if isinstance(value, str) and len(value) <= 512:
        return 'string:' + hashlib.sha256(value.encode()).hexdigest()
    return None


def family(provider: str | None) -> str | None:
    """Provider family as the runtime groups it: every OpenAI route shares one."""
    if provider is None:
        return None
    return OPENAI_FAMILY if provider == OPENAI_FAMILY else provider


def normalize_tier(value: Any) -> str | None:
    """None means standard routing. 'fast' is the legacy spelling of 'priority'."""
    tier = _token(value, 40)
    if tier in (None, 'default'):
        return None
    return 'priority' if tier == 'fast' else tier


def _counts(value: Any) -> tuple[int, int, int, int, int] | None:
    if not isinstance(value, dict):
        return None
    result = []
    for name in ('inputTokens', 'cachedInputTokens', 'outputTokens', 'reasoningOutputTokens', 'cacheWriteInputTokens'):
        number = value.get(name, 0)
        if type(number) is not int or number < 0 or number > 10 ** 12:
            return None
        result.append(number)
    return tuple(result)


def account_tag(environment: dict) -> str | None:
    """First 12 hex of the profile's account fingerprint, or the external provider id.

    The fingerprint is a domain-separated SHA-256 of the account id, not a
    credential (proxy_auth). Two profiles with one tag share one prompt cache.
    """
    fingerprint = environment.get('CODEX_MANAGER_EXPECTED_ACCOUNT_FINGERPRINT')
    if isinstance(fingerprint, str) and len(fingerprint) >= 12 and all(c in '0123456789abcdef' for c in fingerprint[:12].lower()):
        return fingerprint[:12].lower()
    raw = environment.get('CODEX_MANAGER_PRIMARY_MODEL')
    if raw:
        try:
            binding = json.loads(raw)
        except ValueError:
            return None
        if isinstance(binding, dict) and _token(binding.get('model_provider')):
            return 'provider:' + binding['model_provider']
    return None


class _Thread:
    __slots__ = ('provider', 'model', 'effort', 'tier', 'turn_tier', 'baseline', 'baseline_turn', 'raw', 'replay',
                 'sub')

    def __init__(self):
        self.provider = self.model = self.effort = self.tier = self.turn_tier = None
        self.baseline: tuple | None = None
        # Turn of the event that set the baseline. A gap between totals is only
        # this profile's missed requests within one turn: before a turn, a
        # shared-history reload re-seeds the totals with other profiles' usage.
        self.baseline_turn: str | None = None
        self.raw = False
        # A resume/fork response is followed by one replayed snapshot. After a
        # shared-history reload it carries another profile's totals and last
        # request, so it resets the baseline and is never a row here.
        self.replay = False
        # A sub-agent thread (thread.parentThreadId). The warmth summary never
        # ranks these among the recent tasks a card may show.
        self.sub = False


class UsageObserver:
    """Reduce mirrored app-server traffic to request rows. Pure logic, no I/O."""

    def __init__(self, profile_id: str, account: str | None, emit: Callable[[dict, str], None],
                 clock: Callable[[], float] = time.time):
        self.profile_id = profile_id
        self.account = account
        self.emit = emit
        self.clock = clock
        self.threads: OrderedDict[str, _Thread] = OrderedDict()
        self.pending: OrderedDict[str, tuple] = OrderedDict()
        self.skipped = 0

    def _thread(self, thread_id: str) -> _Thread:
        state = self.threads.get(thread_id)
        if state is None:
            state = self.threads[thread_id] = _Thread()
            while len(self.threads) > MAX_TRACKED_THREADS:
                self.threads.popitem(last=False)
        else:
            self.threads.move_to_end(thread_id)
        return state

    def consume(self, direction: str, message: Any) -> None:
        if not isinstance(message, dict):
            return
        method = message.get('method')
        params = message.get('params')
        params = params if isinstance(params, dict) else {}
        if direction == 'client':
            if not isinstance(method, str):
                return
            thread_id = _token(params.get('threadId'))
            if method in _TRACKED_REQUESTS:
                key = _request_key(message.get('id'))
                if key is not None:
                    self.pending[key] = (method, thread_id, _overrides(params))
                    while len(self.pending) > MAX_PENDING:
                        self.pending.popitem(last=False)
            elif method == 'turn/start' and thread_id:
                state = self._thread(thread_id)
                _apply(state, _overrides(params))
                turn_tier = params.get('serviceTierForTurn')
                state.turn_tier = ('default' if turn_tier == 'default' else normalize_tier(turn_tier)) if turn_tier is not None else None
            return
        if method is None:
            key = _request_key(message.get('id'))
            request = self.pending.pop(key, None) if key is not None else None
            if request is not None and 'error' not in message and isinstance(message.get('result'), dict):
                self._response(request, message['result'])
            return
        if 'id' in message:
            return  # Server-to-client request; nothing about usage.
        if method == 'thread/started':
            # New threads, forks and sub-agents. Durable metadata only fills
            # what this proxy has not observed from a response or setting.
            thread = params.get('thread')
            thread_id = _token(thread.get('id')) if isinstance(thread, dict) else None
            if thread_id:
                state = self._thread(thread_id)
                if _token(thread.get('parentThreadId')):
                    state.sub = True
                if state.provider is None and state.model is None:
                    _apply(state, dict(provider=thread.get('modelProvider'), model=thread.get('model'),
                                       effort=thread.get('reasoningEffort')))
            return
        thread_id = _token(params.get('threadId'))
        if not thread_id:
            return
        if method == 'thread/tokenUsage/updated':
            self._usage(thread_id, params)
        elif method == 'rawResponse/completed':
            self._raw(thread_id, params)
        elif method == 'thread/settings/updated':
            settings = params.get('threadSettings')
            if isinstance(settings, dict):
                _apply(self._thread(thread_id), dict(
                    provider=settings.get('modelProvider'), model=settings.get('model'),
                    effort=settings.get('effort'), tier=('set', settings.get('serviceTier'))))
        elif method == 'turn/started':
            state = self.threads.get(thread_id)
            if state is not None:
                state.replay = False  # No replay came (excludeTurns); requests follow.
        elif method == 'turn/completed':
            state = self.threads.get(thread_id)
            if state is not None:
                state.turn_tier = None

    def _response(self, request: tuple, result: dict) -> None:
        method, thread_id, overrides = request
        if method in ('thread/start', 'thread/resume', 'thread/fork'):
            thread = result.get('thread')
            thread_id = _token(thread.get('id')) if isinstance(thread, dict) else None
            if not thread_id:
                return
            state = self._thread(thread_id)
            _apply(state, dict(provider=result.get('modelProvider'), model=result.get('model'),
                               effort=result.get('reasoningEffort'), tier=('set', result.get('serviceTier'))))
            if method == 'thread/start':
                # A new thread has no restored usage, so its first real total
                # is a request. Resume and fork wait for the replayed snapshot.
                state.baseline = (0, 0, 0, 0, 0)
            else:
                state.replay = True
        elif thread_id:
            _apply(self._thread(thread_id), overrides)

    def _usage(self, thread_id: str, params: dict) -> None:
        usage = params.get('tokenUsage')
        if not isinstance(usage, dict):
            return
        total, last = _counts(usage.get('total')), _counts(usage.get('last'))
        if total is None or last is None:
            return
        state = self._thread(thread_id)
        turn = _token(params.get('turnId'))
        baseline, state.baseline = state.baseline, total
        baseline_turn, state.baseline_turn = state.baseline_turn, turn
        replay, state.replay = state.replay, False
        if replay or last[0] <= 0 or baseline is None or total[0] <= baseline[0]:
            # Restored snapshot, synthetic estimate (provider switch,
            # compaction, reload), rate-limit re-emission or a reset total.
            self.skipped += 1
            return
        if state.raw:
            return  # Exact rawResponse rows already describe this thread.
        row = self._row(thread_id, state, last, params, 'usage')
        missed = total[0] - baseline[0] - last[0]
        if missed > 0 and turn is not None and turn == baseline_turn:
            # Totals advanced by more than one response within this turn.
            # Across turns the gap may be another profile's requests that an
            # in-place shared-history reload folded into the totals.
            row['unlogged_input'] = missed
        window = usage.get('modelContextWindow')
        if type(window) is int and 0 < window <= 100_000_000:
            row['window'] = window
        self.emit(row, thread_id)

    def _raw(self, thread_id: str, params: dict) -> None:
        counts = _counts(params.get('usage'))
        if counts is None or counts[0] <= 0:
            return
        state = self._thread(thread_id)
        state.raw = True
        self.emit(self._row(thread_id, state, counts, params, 'raw'), thread_id)

    def _row(self, thread_id: str, state: _Thread, counts: tuple, params: dict, kind: str) -> dict:
        tier = state.tier if state.turn_tier is None else (None if state.turn_tier == 'default' else state.turn_tier)
        row = {'v': LEDGER_VERSION, 'ts': round(self.clock(), 3), 'profile': self.profile_id,
               'account': self.account, 'family': family(state.provider), 'provider': state.provider,
               'model': state.model, 'effort': state.effort, 'tier': tier,
               'thread': thread_hash(thread_id), 'input': counts[0], 'cached': counts[1],
               'output': counts[2], 'reasoning': counts[3], 'kind': kind}
        if counts[4]:
            row['cache_write'] = counts[4]
        turn = _token(params.get('turnId'))
        if turn:
            row['turn'] = _turn_hash(turn)
        if state.sub:
            row['sub'] = 1
        return row


def _overrides(params: dict) -> dict:
    """Settings a request asks for: model, effort, tier ('set', value) and provider."""
    result = dict(provider=params.get('modelProvider'), model=params.get('model'), effort=params.get('effort'))
    mode = params.get('collaborationMode')
    settings = mode.get('settings') if isinstance(mode, dict) else None
    if isinstance(settings, dict):
        # A collaboration mode takes precedence over model and effort.
        result['model'] = settings.get('model') or result['model']
        result['effort'] = settings.get('reasoning_effort') or result['effort']
    config = params.get('config')
    if isinstance(config, dict):
        result['provider'] = result['provider'] or config.get('model_provider')
        result['effort'] = result['effort'] or config.get('model_reasoning_effort')
    if 'serviceTier' in params:
        result['tier'] = ('set', params['serviceTier'])
    return result


def _apply(state: _Thread, values: dict) -> None:
    if _token(values.get('provider')):
        state.provider = values['provider']
    if _token(values.get('model')):
        state.model = values['model']
    if _token(values.get('effort'), 20):
        state.effort = values['effort']
    tier = values.get('tier')
    if isinstance(tier, tuple):
        state.tier = normalize_tier(tier[1])


class ServeLedger:
    """Bounded, content-free per-profile ledger with a background writer.

    ``consume`` runs on the proxy's frame loop under the protocol lock; it only
    updates memory and enqueues. File appends, rotation and the settings file
    happen on one worker thread, so a slow disk never blocks runtime traffic.
    """

    def __init__(self, directory: Path | str, profile_id: str, account: str | None = None, *,
                 max_bytes: int = MAX_LEDGER_BYTES, clock: Callable[[], float] = time.time, start: bool = True):
        self.directory = Path(directory)
        self.path = self.directory / LEDGER_FILE
        self.old_path = self.directory / LEDGER_OLD_FILE
        self.settings_path = self.directory / SETTINGS_FILE
        self.max_bytes = max_bytes
        self.clock = clock
        self.lock = threading.Lock()
        # Serializes file writes between the worker and flush()/close() callers.
        self.write_lock = threading.Lock()
        self.queue: queue.Queue = queue.Queue(maxsize=1024)
        self.rows = self.dropped = self.write_errors = 0
        self.settings: dict[str, dict] = _load_settings(self.settings_path)
        self.settings_dirty = False
        self.settings_written_at = 0.0
        self.observer = UsageObserver(profile_id, account, self._emit, clock)
        self.worker = None
        if start:
            self.worker = threading.Thread(target=self._run, name='codex-serve-ledger', daemon=True)
            self.worker.start()

    def consume(self, direction: str, message: Any) -> None:
        try:
            self.observer.consume(direction, message)
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
            pass  # Accounting must never disturb runtime traffic.

    def _emit(self, row: dict, thread_id: str) -> None:
        provider = row.get('provider')
        if provider:
            # The account tag decides whether another profile's tier may be
            # restored here (ProviderReturn); effort is per thread, tier per account.
            entry = {'model': row.get('model'), 'effort': row.get('effort'), 'tier': row.get('tier'), 'at': row['ts'],
                     'account': row.get('account')}
            with self.lock:
                # Kept in update order (also in the file), so the thread to
                # drop is the first one; no scan on the frame loop.
                providers = self.settings.pop(row['thread'], {})
                providers[provider] = entry
                self.settings[row['thread']] = providers
                while len(self.settings) > MAX_SETTINGS_THREADS:
                    del self.settings[next(iter(self.settings))]
                self.settings_dirty = True
        try:
            self.queue.put_nowait(row)
        except queue.Full:
            self.dropped += 1

    def provider_settings(self, thread_id: str, provider: str) -> dict | None:
        with self.lock:
            entry = self.settings.get(thread_hash(thread_id), {}).get(provider)
            return dict(entry) if isinstance(entry, dict) else None

    @property
    def account(self) -> str | None:
        return self.observer.account

    def status(self) -> dict:
        return {'version': LEDGER_VERSION, 'rows': self.rows, 'dropped': self.dropped,
                'write_errors': self.write_errors, 'skipped_usage_events': self.observer.skipped}

    def _run(self) -> None:
        while True:
            try:
                batch = [self.queue.get(timeout=1.0)]
            except queue.Empty:
                batch = []
            while len(batch) < 256:
                try:
                    batch.append(self.queue.get_nowait())
                except queue.Empty:
                    break
            stop = any(item is _STOP for item in batch)
            rows = [item for item in batch if item is not _STOP]
            if rows:
                self._write_rows(rows)
            self._write_settings(force=stop)
            for _ in batch:
                self.queue.task_done()
            if stop:
                return

    def flush(self) -> None:
        """Write everything queued so far (tests and orderly shutdown)."""
        if self.worker is None:
            rows = []
            while True:
                try:
                    rows.append(self.queue.get_nowait())
                except queue.Empty:
                    break
                self.queue.task_done()
            if rows:
                self._write_rows(rows)
            self._write_settings(force=True)
        else:
            self.queue.join()
            self._write_settings(force=True)

    def close(self, timeout: float = 2.0) -> None:
        if self.worker is None:
            self.flush()
            return
        try:
            self.queue.put(_STOP, timeout=timeout)
        except queue.Full:
            pass
        self.worker.join(timeout=timeout)

    def _write_rows(self, rows: list[dict]) -> None:
        data = b''.join(json.dumps(row, separators=(',', ':'), ensure_ascii=True).encode('ascii') + b'\n' for row in rows)
        with self.write_lock:
            self._append(data, len(rows))

    def _append(self, data: bytes, count: int) -> None:
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            size = self.path.stat().st_size if self.path.exists() else 0
            if size and size + len(data) > self.max_bytes:
                try:
                    os.replace(self.path, self.old_path)
                    size = 0
                except OSError:
                    # A reader may hold the file briefly on Windows; retry on the
                    # next batch, but never let the file grow without bound.
                    if size + len(data) > 2 * self.max_bytes:
                        self.dropped += count
                        return
            with open(self.path, 'ab') as stream:
                stream.write(data)
            self.rows += count
        except OSError:
            self.write_errors += 1
            self.dropped += count

    def _write_settings(self, force: bool = False) -> None:
        with self.lock:
            if not self.settings_dirty:
                return
            if not force and self.clock() - self.settings_written_at < SETTINGS_WRITE_INTERVAL:
                return
            # Serialized under the lock instead of a deep copy: one pass, and the
            # frame loop waits for it at most once per write interval.
            body = json.dumps({'version': LEDGER_VERSION, 'threads': self.settings}, separators=(',', ':')).encode('ascii')
            self.settings_dirty = False
            self.settings_written_at = self.clock()
        temporary = self.settings_path.with_name(self.settings_path.name + '.tmp-' + str(os.getpid()))
        try:
            with self.write_lock:
                self.directory.mkdir(parents=True, exist_ok=True)
                temporary.write_bytes(body)
                os.replace(temporary, self.settings_path)
        except OSError:
            self.write_errors += 1
            with self.lock:
                self.settings_dirty = True
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _clean_providers(providers: Any) -> dict[str, dict]:
    if not isinstance(providers, dict):
        return {}
    return {provider: entry for provider, entry in providers.items()
            if _token(provider) and isinstance(entry, dict) and isinstance(entry.get('at'), (int, float))
            and not isinstance(entry.get('at'), bool)}


def _load_settings(path: Path) -> dict[str, dict]:
    try:
        if not path.is_file() or path.stat().st_size > MAX_SETTINGS_BYTES:
            return {}
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError, UnicodeError):
        return {}
    threads = data.get('threads') if isinstance(data, dict) else None
    if not isinstance(threads, dict):
        return {}
    result = {}
    # The writer keeps threads in update order: the newest are last.
    for key, providers in list(threads.items())[-MAX_SETTINGS_THREADS:]:
        if isinstance(key, str) and len(key) == 24:
            clean = _clean_providers(providers)
            if clean:
                result[key] = clean
    return result


_DECODER = json.JSONDecoder()


def _thread_settings(text: str, key: str) -> dict[str, dict]:
    """Decode only one thread's object from a settings file body.

    Thread keys are 24-hex digests, so the key is found as text without
    parsing the other threads. A match counts only as an object member name
    (after ``{`` or ``,``, followed by ``:``) whose value is a providers
    object; the same text at another depth decodes to nothing and is skipped.
    """
    needle = '"' + key + '"'
    position = text.find(needle)
    while position > 0:
        before = position - 1
        while before >= 0 and text[before] in ' \t\r\n':
            before -= 1
        after = position + len(needle)
        while after < len(text) and text[after] in ' \t\r\n':
            after += 1
        if before >= 0 and text[before] in '{,' and text.startswith(':', after):
            after += 1
            while after < len(text) and text[after] in ' \t\r\n':
                after += 1
            try:
                value, _ = _DECODER.raw_decode(text, after)
            except (ValueError, RecursionError):
                return {}
            providers = _clean_providers(value)
            if providers:
                return providers
        position = text.find(needle, position + 1)
    return {}


class SettingsLookup:
    """Other profiles' settings for one thread, without parsing whole files.

    Each lookup lists the instance directories and stats their settings files.
    A file is read only when it changed since this thread was last looked up
    in it, and then only the thread's own object is decoded. The proxy calls
    this on its frontend reader before taking the protocol lock, so the
    runtime-to-app stream never waits for it.
    """

    MAX_DIRECTORIES = 256
    MAX_CACHE = 1024

    def __init__(self, instances: Path | str | None, exclude: str | None = None):
        self.instances = Path(instances) if instances is not None else None
        self.exclude = exclude
        self.cache: OrderedDict[tuple, dict] = OrderedDict()
        self.lock = threading.Lock()
        self.reads = 0

    def entries(self, key: str) -> list[dict[str, dict]]:
        if self.instances is None:
            return []
        try:
            with os.scandir(self.instances) as listing:
                names = sorted(entry.name for entry in listing if entry.is_dir())
        except OSError:
            return []
        result = []
        with self.lock:
            for name in names[:self.MAX_DIRECTORIES]:
                if name == self.exclude:
                    continue  # This profile's own settings are the ledger's memory.
                path = self.instances / name / SETTINGS_FILE
                try:
                    info = os.stat(path)
                except OSError:
                    continue
                if info.st_size > MAX_SETTINGS_BYTES:
                    continue
                cache_key = (name, info.st_mtime_ns, info.st_size, key)
                providers = self.cache.get(cache_key)
                if providers is None:
                    try:
                        text = path.read_bytes().decode('utf-8')
                    except (OSError, UnicodeError):
                        continue
                    self.reads += 1
                    providers = self.cache[cache_key] = _thread_settings(text, key)
                    while len(self.cache) > self.MAX_CACHE:
                        self.cache.popitem(last=False)
                else:
                    self.cache.move_to_end(cache_key)
                if providers:
                    result.append(providers)
        return result


def lookup_provider_settings(instances: Path | str | None, thread_id: str, provider: str,
                             local: ServeLedger | None = None) -> dict | None:
    """Newest settings any profile recorded for (thread, provider) (diagnostics)."""
    return ProviderReturn(instances, local).settings(thread_id, provider)[0]


class ProviderReturn:
    """Restore a provider's last effort/tier when the adapter marks a provider return.

    Only the marked thread/resume|fork requests change, and only when the stored
    model is the model the request opens with (effort is per model). Effort
    comes from the newest entry any profile recorded for (thread, provider).
    Tier is billing and routing per account: it comes only from an entry this
    profile or another profile on the same account recorded, and never replaces
    a tier the request already carries. Without stored settings the adapter's
    profile defaults stay, as before this change.
    """

    def __init__(self, instances: Path | str | None, local: ServeLedger | None = None,
                 account: str | None = None, exclude: str | None = None):
        self.local = local
        self.account = account if account is not None else (local.account if local is not None else None)
        self.lookup = SettingsLookup(instances, exclude)
        self.restored = 0

    def settings(self, thread_id: str, provider: str) -> tuple[dict | None, dict | None]:
        """(newest entry for effort, newest entry from this profile's account for tier)."""
        candidates: list[tuple[bool, dict]] = []
        own = self.local.provider_settings(thread_id, provider) if self.local is not None else None
        if own is not None:
            candidates.append((True, own))
        for providers in self.lookup.entries(thread_hash(thread_id)):
            entry = providers.get(provider)
            if entry is not None:
                candidates.append((self.account is not None and entry.get('account') == self.account, entry))
        newest = max(candidates, key=lambda item: item[1]['at'], default=(False, None))[1]
        same = max((item for item in candidates if item[0]), key=lambda item: item[1]['at'], default=(False, None))[1]
        return newest, same

    def __call__(self, message: Any) -> Any:
        if not isinstance(message, dict) or message.get('method') not in ('thread/resume', 'thread/fork'):
            return message
        params = message.get('params')
        if not isinstance(params, dict) or PROVIDER_RETURN_MARKER not in params:
            return message
        result = copy.deepcopy(message)
        params = result['params']
        params.pop(PROVIDER_RETURN_MARKER, None)
        config = params.get('config') if isinstance(params.get('config'), dict) else {}
        thread_id = _token(params.get('threadId'))
        provider = _token(params.get('modelProvider')) or _token(config.get('model_provider'))
        model = params.get('model') or config.get('model')
        if not thread_id or not provider or not _token(model):
            return result
        try:
            newest, same = self.settings(thread_id, provider)
        except (OSError, ValueError, TypeError, KeyError):
            newest = same = None
        changed = False
        if newest and newest.get('model') == model and newest.get('effort') in EFFORTS:
            config = params['config'] = dict(config)
            config['model_reasoning_effort'] = newest['effort']
            changed = True
        if same and same.get('model') == model and 'tier' in same and 'serviceTier' not in params:
            # null asks for explicit standard routing; the runtime omits it
            # from the request, exactly like an unset tier.
            params['serviceTier'] = normalize_tier(same.get('tier'))
            changed = True
        if changed:
            self.restored += 1
        return result
