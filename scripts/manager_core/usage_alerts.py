"""Account usage alerts from the usage the manager already shows (revision 124).

For every account (Codex/GPT and Claude) the 5-hour and weekly windows of the presented usage
raise one alert per window and reset period when they cross 90%, when they reach 100% (or a
Claude turn stops on the usage limit) and when that reset time passes ("다시 사용 가능").
No network call: only profile usage values already in the state and the usage-limit stops
recorded for automatic continuation. Delivered alerts are kept per window and period so a
manager restart never repeats them; the first evaluation only records what is already true.
"""
from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import threading
import time
from uuid import uuid4

WINDOWS = {'5시간': '5시간', '주간': '주간'}
CLAUDE_WINDOWS = {'five_hour': '5시간', 'seven_day': '주간'}
STOP_WINDOWS = {'session': '5시간', 'weekly': '주간', 'opus': '주간', 'sonnet': '주간', 'model': '주간'}
STOP_LABELS = {'session': '5시간 한도', 'weekly': '주간 한도', 'opus': '주간 Opus 한도', 'sonnet': '주간 Sonnet 한도',
               'model': '모델 사용 한도', 'credits': '사용 크레딧', 'unknown': '사용 한도'}
WARN_PERCENT = 90
PERIOD_SECONDS = 600       # Reset times of one window period agree within this bucket.
SEEN_RETAIN = 9 * 86400
EVENT_RETAIN = 24 * 3600
MAX_EVENTS = 32


def _when(epoch):
    moment = datetime.fromtimestamp(epoch).astimezone()
    return (moment.strftime('%H:%M') if moment.date() == datetime.now().astimezone().date()
            else moment.strftime('%m/%d %H:%M'))


def _windows(usage, now):
    """(label, used percent, reset epoch) of fresh 5-hour/weekly windows."""
    if not isinstance(usage, dict):
        return []
    result = []
    for window in usage.get('windows') or []:
        # Claude windows carry their own freshness; Codex windows share the reply's.
        if not isinstance(window, dict) or window.get('freshness', usage.get('freshness')) in ('stale', 'unknown'):
            continue
        label = CLAUDE_WINDOWS.get(window.get('key')) or WINDOWS.get(window.get('label'))
        used, reset = window.get('used_percent'), window.get('resets_at')
        if (label and type(used) in (int, float) and not isinstance(used, bool)
                and type(reset) is int and reset > now):
            result.append((label, float(used), reset))
    return result


class UsageAlerts:
    def __init__(self, root, *, clock=time.time):
        self.path = Path(root) / 'work/control-center/usage-alerts.json'
        self.clock = clock
        self.lock = threading.Lock()
        try:
            value = json.loads(self.path.read_text(encoding='utf-8'))
            if value.get('version') != 1 or not isinstance(value.get('seen'), dict):
                raise ValueError()
            self.state, self.baseline = value, False
        except (OSError, ValueError, AttributeError):
            # No history yet: record what is already true without alerting about it.
            self.state, self.baseline = dict(version=1, seen={}, pending={}, events=[]), True
        self.state.setdefault('pending', {})
        self.state.setdefault('events', [])
        # A first run with nothing to record writes nothing.
        self._saved = json.dumps(self.state, ensure_ascii=False, sort_keys=True)

    def _save(self):
        # Its own small file, written only when its content changed; never the manager store.
        payload = json.dumps(self.state, ensure_ascii=False, sort_keys=True)
        if payload == self._saved:
            return
        temporary = self.path.with_name(self.path.name + '.' + str(os.getpid()) + '.tmp')
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(payload, encoding='utf-8')
            os.replace(temporary, self.path)
            self._saved = payload
        except OSError:
            pass  # Retried with the next change.

    def _emit(self, key, profile, kind, title, body, **extra):
        seen = self.state['seen']
        if key in seen:
            return False
        now = self.clock()
        seen[key] = now
        if not self.baseline:
            self.state['events'].append(dict(id=uuid4().hex, at=now, profile_id=profile['id'],
                                             alias=profile.get('alias') or profile['id'][:8], kind=kind,
                                             title=title, body=body, **extra))
        return True

    def evaluate(self, profiles, stops=()):
        """New alerts for these presented profiles; returns the recent alert list."""
        with self.lock:
            now, changed = self.clock(), self.baseline
            live = {p['id']: p for p in profiles if isinstance(p, dict) and isinstance(p.get('id'), str)
                    and not p.get('removed_at')}
            pending = self.state['pending']
            for stop in stops:
                profile = live.get(stop.get('profile_id'))
                if profile is None or now - stop.get('stopped_at', 0) > EVENT_RETAIN:
                    continue
                kind, reset = stop.get('kind'), stop.get('resets_at')
                label = STOP_WINDOWS.get(kind)
                body = STOP_LABELS.get(kind, '사용 한도')
                body += ' · ' + (_when(reset) + ' 재설정' if reset else '재설정 시각 미확인')
                if stop.get('status') == 'scheduled' and stop.get('due_at'):
                    body += ' · ' + _when(stop['due_at']) + '쯤 자동으로 이어 합니다.'
                elif stop.get('reason') == 'disabled' or reset is None:
                    body += ' · 재설정 뒤 메시지를 보내 이어 가세요.'
                extra = dict(thread_id=stop.get('thread_id'), host_id=stop.get('host_id'))
                changed |= self._emit('stop:' + stop.get('key', ''), profile, 'stopped',
                                      'Claude 작업이 사용 한도로 멈췄습니다', body, **extra)
                if label and reset and reset > now:
                    period = reset // PERIOD_SECONDS
                    # The stop alert already says that this window is used up.
                    for level in ('limit', 'warn'):
                        key = '|'.join((profile['id'], label, str(period), level))
                        if key not in self.state['seen']:
                            self.state['seen'][key], changed = now, True
                    slot = profile['id'] + '|' + label
                    if slot not in pending:
                        pending[slot], changed = dict(profile_id=profile['id'], label=label, resets_at=reset), True
            for profile in live.values():
                for label, used, reset in _windows(profile.get('usage'), now):
                    base = '|'.join((profile['id'], label, str(reset // PERIOD_SECONDS)))
                    if used >= 100:
                        changed |= self._emit(base + '|limit', profile, 'limit', label + ' 한도 도달',
                                              f'{_when(reset)}에 재설정됩니다.')
                        self.state['seen'].setdefault(base + '|warn', now)
                        slot = profile['id'] + '|' + label
                        if pending.get(slot, {}).get('resets_at') != reset:
                            pending[slot] = dict(profile_id=profile['id'], label=label, resets_at=reset)
                            changed = True
                    elif used >= WARN_PERCENT:
                        changed |= self._emit(base + '|warn', profile, 'warn', f'{label} 한도 {used:.0f}% 사용',
                                              f'{_when(reset)}에 재설정됩니다.')
            for slot, value in list(pending.items()):
                profile = live.get(value.get('profile_id'))
                if profile is None:
                    pending.pop(slot)
                    changed = True
                elif value.get('resets_at', 0) <= now:
                    pending.pop(slot)
                    key = '|'.join((profile['id'], value['label'], str(value['resets_at'] // PERIOD_SECONDS), 'reset'))
                    self._emit(key, profile, 'reset', '다시 사용 가능', value['label'] + ' 한도가 재설정되었습니다.')
                    changed = True
            seen = self.state['seen']
            for key in [key for key, at in seen.items() if now - at > SEEN_RETAIN]:
                seen.pop(key)
                changed = True
            events = [event for event in self.state['events'] if now - event.get('at', 0) <= EVENT_RETAIN][-MAX_EVENTS:]
            if events != self.state['events']:
                self.state['events'], changed = events, True
            self.baseline = False
            if changed:
                self._save()
            return [dict(event) for event in self.state['events']]

    @staticmethod
    def attention(profile, scheduled=None, now=None):
        """The card's short emphasis for an account at or near its usage limit, or None."""
        now = time.time() if now is None else now
        windows = _windows(profile.get('usage'), now)
        full = [window for window in windows if window[1] >= 100]
        if full or scheduled:
            parts = [f'{label} 한도 도달 · {_when(reset)} 재설정' for label, _, reset in full]
            if scheduled and scheduled.get('due_at'):
                parts.append('멈춘 작업 자동 이어하기 · ' + _when(scheduled['due_at']))
            return dict(level='limit', short='한도 도달', detail=' · '.join(parts) or '사용 한도 도달')
        near = [window for window in windows if window[1] >= WARN_PERCENT]
        if near:
            label, used, reset = max(near, key=lambda window: window[1])
            return dict(level='warn', short=f'한도 {used:.0f}%',
                        detail=f'{label} 한도 {used:.0f}% 사용 · {_when(reset)} 재설정')
        return None
