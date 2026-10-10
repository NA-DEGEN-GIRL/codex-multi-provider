"""Continue a Claude turn automatically once the account's usage limit has reset (revision 124).

The Claude runner ends a turn refused by the subscription limit with a fixed English message
that carries a machine tag, ``[usage-limit: <kind>, resets <UTC ISO time>]``. The runtime turns
it into the turn's error. The managed runtime proxy (local) and the SSH pump observe that error
on the app-server stream and drop one small content-free file per stopped turn into
``work/control-center/usage-continuations/inbox``. This scheduler, in the manager backend,
schedules one continuation per stop at the reset time plus a short jitter, persists it, and at
that time sends the fixed continuation message into the same task through the profile's
authenticated runtime admin endpoint (``turn/start`` restricted to that one message), after
checking that nothing changed: the profile still exists and allows it, the task is loaded and
idle, its newest turn is still the stopped one, and the usage window is no longer exhausted
(otherwise it is rescheduled to the new reset, at most three times). Every decision is logged.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import random
import re
import threading
import time
from uuid import UUID, uuid4

TAG = re.compile(r'\[usage-limit: (?P<kind>[a-z]{1,16})(?:, resets (?P<at>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z))?\]')
MARKER = 'Claude usage limit reached'
KINDS = ('session', 'weekly', 'opus', 'sonnet', 'model', 'credits', 'unknown')
# Which usage window a stop belongs to; credits and unknown limits have no window to check.
WINDOW = {'session': 'five_hour', 'weekly': 'seven_day', 'opus': 'seven_day', 'sonnet': 'seven_day',
          'model': 'seven_day'}
TURN_ID = re.compile(r'[A-Za-z0-9_-]{1,128}')
HOST = re.compile(r'local|ssh:[A-Za-z0-9][A-Za-z0-9_.-]{0,127}')
JITTER = (60.0, 120.0)
MAX_ATTEMPTS = 3            # Reschedules while the limit is still reported exhausted.
CLOSED_GRACE = 15 * 60      # How long a due continuation waits for the profile's runtime to reopen.
MAX_OVERDUE = 12 * 3600     # A continuation this late (manager closed meanwhile) is not sent.
MAX_AHEAD = 8 * 86400       # A reset further out than this is not scheduled.
CHAIN_WINDOW = 10 * 60      # A sent continuation that stops again this soon counts as a retry.
RETAIN = 7 * 86400
MAX_ENTRIES = 256
MAX_EVENTS = 100
LOG_LIMIT = 512 * 1024

REASONS = {
    'disabled': '이 프로필은 한도 재설정 후 자동 이어하기가 꺼져 있습니다.',
    'reset_unknown': '재설정 시각을 알 수 없어 자동으로 이어 하지 않습니다. 재설정 뒤 메시지를 보내 주세요.',
    'reset_too_far': '재설정까지 너무 오래 남아 자동으로 이어 하지 않습니다.',
    'profile_removed': '프로필이 제거되어 자동 이어하기를 취소했습니다.',
    'not_claude_profile': 'Claude 프로필의 작업이 아니어서 자동으로 이어 하지 않습니다.',
    'new_message': '작업에 새 메시지가 있어 자동 이어하기를 건너뛰었습니다.',
    'thread_running': '작업이 실행 중이어서 자동 이어하기를 건너뛰었습니다.',
    'thread_not_open': '작업이 열려 있지 않아 자동 이어하기를 건너뛰었습니다.',
    'turn_changed': '멈춘 턴의 상태가 바뀌어 자동 이어하기를 건너뛰었습니다.',
    'profile_closed': '프로필이 닫혀 있어 자동 이어하기를 건너뛰었습니다.',
    'still_limited': '한도가 계속 소진 상태여서 자동 이어하기를 멈췄습니다.',
    'runtime_outdated': '실행 중인 관리 런타임이 자동 이어하기를 지원하지 않습니다. 관리 앱 업데이트 뒤 프로필을 다시 열어 주세요.',
    'send_failed': '자동 이어하기 메시지를 보내지 못했습니다.',
    'send_uncertain': '자동 이어하기 전송 결과를 확인하지 못했습니다. 다시 보내지 않습니다.',
    'expired': '예약 시각이 너무 지나 자동 이어하기를 건너뛰었습니다.',
}


def parse_stop(message):
    """{kind, resets_at} of a runner usage-limit stop message, or None."""
    if not isinstance(message, str) or MARKER not in message or len(message) > 8192:
        return None
    match = TAG.search(message)
    if match is None:
        return None
    kind = match.group('kind') if match.group('kind') in KINDS else 'unknown'
    resets_at = None
    if match.group('at'):
        try:
            resets_at = int(datetime.strptime(match.group('at'), '%Y-%m-%dT%H:%M:%SZ')
                            .replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            resets_at = None
    return dict(kind=kind, resets_at=resets_at)


def directory(root):
    return Path(root) / 'work/control-center/usage-continuations'


def _key(profile_id, host_id, thread_id, turn_id):
    return hashlib.sha256('\0'.join((profile_id, host_id, thread_id, turn_id)).encode()).hexdigest()[:32]


def _valid_stop(profile_id, host_id, stop):
    try:
        profile_id = str(UUID(profile_id))
        thread_id = str(UUID(stop['thread_id']))
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    turn_id, kind, resets_at, observed = (stop.get('turn_id'), stop.get('kind'), stop.get('resets_at'),
                                          stop.get('observed_at'))
    if (not isinstance(host_id, str) or not HOST.fullmatch(host_id) or not isinstance(turn_id, str)
            or not TURN_ID.fullmatch(turn_id) or kind not in KINDS
            or (resets_at is not None and (type(resets_at) is not int or not 0 < resets_at < 253402300800))
            or type(observed) not in (int, float) or not 0 < observed < 253402300800):
        return None
    return dict(profile_id=profile_id, host_id=host_id, thread_id=thread_id, turn_id=turn_id,
                kind=kind, resets_at=resets_at, observed_at=float(observed))


def record_stops(root, profile_id, host_id, stops):
    """Called by the runtime proxy and the SSH pump: one inbox file per stopped turn, content-free."""
    inbox = directory(root) / 'inbox'
    written = 0
    for stop in stops:
        value = _valid_stop(profile_id, host_id, stop)
        if value is None:
            continue
        path = inbox / (_key(value['profile_id'], host_id, value['thread_id'], value['turn_id']) + '.json')
        if path.exists():
            continue
        inbox.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + '.' + str(os.getpid()) + '.tmp')
        temporary.write_text(json.dumps(value), encoding='utf-8')
        os.replace(temporary, path)
        written += 1
    return written


def _when(epoch):
    moment = datetime.fromtimestamp(epoch).astimezone()
    today = datetime.now().astimezone().date()
    return moment.strftime('%H:%M') if moment.date() == today else moment.strftime('%m/%d %H:%M')


def _observed(value):
    try:
        moment = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return moment.timestamp() if moment.tzinfo else 0
    except (TypeError, ValueError):
        return 0


def enabled(profile):
    """The per-profile setting; on unless the user turned it off."""
    return profile.get('claude_auto_continue', True) is not False


def admin_client(root, profile, host_id):
    from .runtime_admin import AdminClient
    if host_id == 'local':
        return AdminClient(root, profile['id'], profile['generation'])
    from .ssh_runtime_control import endpoint_id
    return AdminClient(root, endpoint_id(profile['id'], host_id[4:]), profile['generation'])


class UsageContinuations:
    """Persistent schedule of automatic continuations; one per stopped turn."""

    def __init__(self, root, store, *, clock=time.time, admin=None, jitter=None, interval=15):
        self.root, self.store, self.clock = Path(root), store, clock
        self.admin = admin or (lambda profile, host: admin_client(self.root, profile, host))
        self.jitter = jitter or (lambda: random.uniform(*JITTER))
        self.interval = interval
        self.path = directory(root) / 'state.json'
        self.log_path = directory(root) / 'log.jsonl'
        self.lock = threading.RLock()
        self.stopping, self.wake = threading.Event(), threading.Event()
        self.thread = None
        self.state = self._load()

    # Persistence ---------------------------------------------------------------------------
    def _load(self):
        try:
            value = json.loads(self.path.read_text(encoding='utf-8'))
            if value.get('version') == 1 and isinstance(value.get('entries'), dict) and isinstance(value.get('events'), list):
                return value
        except (OSError, ValueError, AttributeError):
            pass
        return dict(version=1, entries={}, events=[])

    def _save(self):
        from .store import atomic_json
        now = self.clock()
        entries = self.state['entries']
        for key, entry in list(entries.items()):
            if entry['status'] != 'scheduled' and now - entry.get('updated_at', 0) > RETAIN:
                entries.pop(key)
        while len(entries) > MAX_ENTRIES:
            entries.pop(min(entries, key=lambda key: entries[key].get('updated_at', 0)))
        self.state['events'] = self.state['events'][-MAX_EVENTS:]
        try:
            atomic_json(self.path, self.state)
        except OSError:
            pass  # Retried with the next change; the in-memory schedule still runs.

    def _log(self, entry, action, reason=None):
        now = self.clock()
        event = dict(id=uuid4().hex, at=now, action=action, profile_id=entry['profile_id'],
                     host_id=entry['host_id'], thread_id=entry['thread_id'], turn_id=entry['turn_id'],
                     kind=entry['kind'], resets_at=entry.get('resets_at'), due_at=entry.get('due_at'),
                     attempts=entry.get('attempts', 0))
        if reason:
            event['reason'] = reason
        event['message'] = self._message(event)
        self.state['events'].append(event)
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            if self.log_path.exists() and self.log_path.stat().st_size > LOG_LIMIT:
                os.replace(self.log_path, self.log_path.with_name('log.1.jsonl'))
            with self.log_path.open('a', encoding='utf-8') as stream:
                stream.write(json.dumps({key: value for key, value in event.items() if key != 'message'}) + '\n')
        except OSError:
            pass
        return event

    @staticmethod
    def _message(event):
        if event['action'] == 'scheduled':
            return '한도 재설정 후 자동 이어하기 예약 · ' + _when(event['due_at'])
        if event['action'] == 'rescheduled':
            return '한도가 아직 소진 상태여서 다음 재설정 뒤로 다시 예약 · ' + _when(event['due_at'])
        if event['action'] == 'waiting':
            return '자동 이어하기 대기 · 프로필의 런타임 연결을 기다립니다.'
        if event['action'] == 'sent':
            return '한도가 재설정되어 작업을 자동으로 이어 보냈습니다.'
        return REASONS.get(event.get('reason'), '자동 이어하기를 건너뛰었습니다.')

    # Inbox ---------------------------------------------------------------------------------
    def ingest(self):
        inbox = directory(self.root) / 'inbox'
        try:
            paths = sorted(inbox.glob('*.json'))[:256]
        except OSError:
            return 0
        added = 0
        with self.lock:
            for path in paths:
                try:
                    if path.stat().st_size > 4096:
                        raise ValueError('oversized stop record')
                    raw = json.loads(path.read_text(encoding='utf-8'))
                    value = _valid_stop(raw.get('profile_id'), raw.get('host_id'), raw) if isinstance(raw, dict) else None
                    if value is not None:
                        added += self._add(value)
                except (OSError, ValueError):
                    pass
                try:
                    path.unlink()
                except OSError:
                    pass
            if added:
                self._save()
        return added

    def _profile(self, profile_id):
        try:
            return self.store.profile(profile_id)
        except (KeyError, ValueError, OSError, RuntimeError):
            return None

    def _add(self, stop):
        entries = self.state['entries']
        key = _key(stop['profile_id'], stop['host_id'], stop['thread_id'], stop['turn_id'])
        if key in entries:
            return 0
        now = self.clock()
        entry = dict(stop, key=key, stopped_at=stop['observed_at'], status='scheduled', attempts=0,
                     created_at=now, updated_at=now)
        entry.pop('observed_at')
        # A continuation that stops again on the limit right after it was sent is the same stop
        # retried, not a new one: it inherits the attempt count, so a chain ends after three.
        previous = next((item for item in entries.values() if item.get('sent_turn_id') == stop['turn_id']
                         and item['thread_id'] == stop['thread_id'] and item['profile_id'] == stop['profile_id']), None)
        if previous is not None and stop['observed_at'] - previous.get('sent_at', 0) < CHAIN_WINDOW:
            entry['attempts'] = previous.get('attempts', 0) + 1
        entries[key] = entry
        profile = self._profile(stop['profile_id'])
        reason = None
        if profile is None or profile.get('removed_at'):
            reason = 'profile_removed'
        elif profile.get('auth_mode') != 'claude_code':
            reason = 'not_claude_profile'
        elif not enabled(profile):
            reason = 'disabled'
        elif stop['resets_at'] is None:
            reason = 'reset_unknown'
        elif stop['resets_at'] > now + MAX_AHEAD:
            reason = 'reset_too_far'
        elif entry['attempts'] > MAX_ATTEMPTS:
            reason = 'still_limited'
        if reason:
            self._finish(entry, 'skipped', reason)
            return 1
        entry['due_at'] = max(stop['resets_at'], now) + self.jitter()
        self._log(entry, 'scheduled')
        return 1

    def _finish(self, entry, status, reason=None):
        entry.update(status=status, updated_at=self.clock())
        if reason:
            entry['reason'] = reason
        self._log(entry, status if status == 'sent' else 'skipped', reason)

    # Scheduling ----------------------------------------------------------------------------
    def _still_limited(self, entry, profile, now):
        """The new reset time while the stop's usage window still reads exhausted."""
        window_key = WINDOW.get(entry['kind'])
        usage = profile.get('usage') if isinstance(profile.get('usage'), dict) else {}
        for window in usage.get('windows') or []:
            if not isinstance(window, dict) or window.get('key') != window_key:
                continue
            used, reset = window.get('used_percent'), window.get('resets_at')
            if (type(used) in (int, float) and used >= 100 and type(reset) is int and reset > now + 30
                    and _observed(window.get('observed_at') or usage.get('observed_at')) >= entry['stopped_at'] - 600):
                return reset
        return None

    def tick(self):
        self.ingest()
        with self.lock:
            due = [entry for entry in self.state['entries'].values()
                   if entry['status'] == 'scheduled' and entry['due_at'] <= self.clock()]
        for entry in sorted(due, key=lambda item: item['due_at']):
            self._process(entry)

    def _process(self, entry):
        now = self.clock()
        with self.lock:
            if entry['status'] != 'scheduled':
                return
            profile = self._profile(entry['profile_id'])
            if now - entry['due_at'] > MAX_OVERDUE:
                return self._done(entry, 'skipped', 'expired')
            if profile is None or profile.get('removed_at'):
                return self._done(entry, 'skipped', 'profile_removed')
            if not enabled(profile):
                return self._done(entry, 'skipped', 'disabled')
            reset = self._still_limited(entry, profile, now)
            if reset is not None:
                entry['attempts'] = entry.get('attempts', 0) + 1
                if entry['attempts'] > MAX_ATTEMPTS:
                    return self._done(entry, 'skipped', 'still_limited')
                entry.update(resets_at=reset, due_at=reset + self.jitter(), updated_at=now)
                self._log(entry, 'rescheduled')
                return self._save()
        # Admin calls happen outside the lock; the entry stays scheduled meanwhile.
        from .runtime_admin import AdminError
        sent = False
        try:
            client = self.admin(profile, entry['host_id'])
            thread = client.request('thread/read', {'threadId': entry['thread_id'], 'includeTurns': False}, 10)['thread']
            status = thread['status']['type']
            if status == 'active':
                return self._settle(entry, 'skipped', 'thread_running')
            if status != 'idle':
                return self._settle(entry, 'skipped', 'thread_not_open')
            turns = client.request('thread/turns/list', {'threadId': entry['thread_id'], 'limit': 1,
                                                         'sortDirection': 'desc', 'itemsView': 'notLoaded'}, 10)['data']
            if not turns or turns[0]['id'] != entry['turn_id']:
                return self._settle(entry, 'skipped', 'new_message')
            if turns[0]['status'] != 'failed':
                return self._settle(entry, 'skipped', 'turn_changed')
            from .runtime_admin import CONTINUATION_TEXT
            sent = True
            started = client.request('turn/start', {'threadId': entry['thread_id'], 'input': [
                {'type': 'text', 'text': CONTINUATION_TEXT, 'text_elements': []}]}, 30)
            with self.lock:
                entry.update(sent_turn_id=started['turn']['id'], sent_at=self.clock())
            return self._settle(entry, 'sent')
        except AdminError as error:
            if sent:
                return self._settle(entry, 'skipped', 'send_uncertain' if error.uncertain else 'send_failed')
            if error.code == 'invalid_request':
                return self._settle(entry, 'skipped', 'runtime_outdated')
            if error.code in ('unavailable', 'stale_runtime', 'not_ready', 'closed', 'timeout', 'busy'):
                with self.lock:
                    if self.clock() - entry['due_at'] < CLOSED_GRACE:
                        if not entry.get('waiting'):
                            entry['waiting'] = True
                            self._log(entry, 'waiting')
                            self._save()
                        return None
                return self._settle(entry, 'skipped', 'profile_closed')
            return self._settle(entry, 'skipped', 'send_failed')
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return self._settle(entry, 'skipped', 'send_uncertain' if sent else 'send_failed')

    def _done(self, entry, status, reason=None):
        self._finish(entry, status, reason)
        self._save()

    def _settle(self, entry, status, reason=None):
        with self.lock:
            if entry['status'] == 'scheduled':
                self._done(entry, status, reason)

    # Views for the Shell ------------------------------------------------------------------
    def events(self, limit=20):
        with self.lock:
            return [dict(event) for event in self.state['events'][-limit:]]

    def entries(self):
        with self.lock:
            return [dict(entry) for entry in self.state['entries'].values()]

    def scheduled(self, profile_id):
        """The earliest pending continuation of a profile, for its card."""
        with self.lock:
            pending = [entry for entry in self.state['entries'].values()
                       if entry['profile_id'] == profile_id and entry['status'] == 'scheduled']
            return dict(min(pending, key=lambda entry: entry['due_at'])) if pending else None

    # Background thread ---------------------------------------------------------------------
    def start(self):
        with self.lock:
            if self.thread is not None:
                return
            self.thread = threading.Thread(target=self._loop, name='usage-continuations', daemon=True)
            self.thread.start()

    def _loop(self):
        while not self.stopping.is_set():
            try:
                self.tick()
            except Exception:
                pass  # One bad pass never stops later continuations; each decision is logged.
            self.wake.wait(self.interval)
            self.wake.clear()

    def shutdown(self):
        self.stopping.set()
        self.wake.set()
