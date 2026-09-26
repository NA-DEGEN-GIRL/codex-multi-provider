"""Prompt-cache warmth per (thread, profile), estimated from the serve ledgers.

Each profile's RuntimeProxy appends content-free request rows to
``instances/<profile>/serve-ledger.jsonl`` (serve_ledger.py). This module reads
those files incrementally and answers, for a recently used thread and every
profile: is the thread's prefix likely cached on that profile's account and
provider, for about how long, and roughly what the first request there costs.

It is a model, not a measurement of the server cache, and every figure is shown
as approximate:

- A profile is warm for a thread when its account (or the same profile) served
  the thread on the same provider family, nothing replaced the history since
  (a later request whose input fell by more than 30% is a compaction), no other
  family served the thread in between (today's runtime rebuilds the context on
  a family change, so the returning family's prefix breaks), and the retention
  for the elapsed time is above the provider's warm threshold.
- First-request cost = (context - warm) x input + warm x cached, times the tier
  multiplier (Fast = 2.5 on GPT) and a cache-write multiplier where one exists.
- Retention and TTL are per provider: GPT from the 14-day research table,
  DeepSeek from its own measurements, Claude with a 5-minute default TTL.

Memory stays bounded per thread: the newest rows, plus the last row of every
(profile or account, family) that left that window and the times of the newest
compaction and family change among those rows. A long stint on one account
therefore never erases another account's anchor.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import threading
import time
from typing import Any, Iterable

from .serve_ledger import LEDGER_FILE, LEDGER_OLD_FILE, OPENAI_FAMILY, normalize_tier, thread_hash

PRICE_FILE = 'cache-prices.json'
MAX_READ_BYTES = 4 * 1024 * 1024
# First look at a profile: tail blocks, newest first, until the report horizon
# is covered or the budget is spent (the rotated file only when needed).
FIRST_BLOCK_BYTES = 256 * 1024
FIRST_READ_BYTES = 1024 * 1024
ROW_HORIZON_SECONDS = 24 * 3600
REPORT_HORIZON_SECONDS = 6 * 3600
MAX_THREADS = 512
MAX_ROWS_PER_THREAD = 64
MAX_ANCHORS_PER_THREAD = 64
MAX_REPORTED_THREADS = 16
MAX_PINNED_THREADS = 64
NOTICE_MIN_CONTEXT = 60_000
COMPACTION_DROP = 0.7
REBUILD_SHARE = 0.6
ASSUMED_PRICE = '가정 단가'

# Prices per 1M tokens. GPT credits come from the rate card; DeepSeek dollars
# are an assumption (cached = 0.1x, output = 1.5x of a $0.28 uncached input)
# until the user records the real rate in work/control-center/cache-prices.json.
# ``window`` is the fallback context window when no row of that family has one.
KINDS: dict[str, dict] = {
    'gpt': dict(unit='credits', input=250.0, cached=25.0, output=1250.0, write=1.0, window=258_400,
                retention=((60, .91), (900, .83), (1800, .67), (7200, .50), (14400, .13)),
                warm_seconds=1800, tiers={'priority': 2.5}),
    'deepseek': dict(unit='usd', input=0.28, cached=0.028, output=0.42, write=1.0, assumed=True,
                     retention=((300, .968), (3600, .915), (21600, .888)), warm_seconds=21600, tiers={}),
    # Anthropic prompt caching: reads ~0.1x, 5-minute writes 1.25x, per workspace.
    'claude': dict(unit=None, write=1.25, retention=((300, 1.0),), warm_seconds=300, tiers={}),
    'other': dict(unit=None, write=1.0, retention=((300, .9),), warm_seconds=300, tiers={}),
}


def kind_of(provider_family: str | None, model: str | None) -> str:
    if provider_family == OPENAI_FAMILY:
        return 'gpt'
    slug = (model or '').lower()
    if 'deepseek' in slug:
        return 'deepseek'
    if 'claude' in slug:
        return 'claude'
    return 'other'


def retention(kind: dict, elapsed: float) -> float:
    if elapsed < 0:
        elapsed = 0
    for limit, value in kind['retention']:
        if elapsed <= limit:
            return value
    return 0.0


def korean_tokens(count: int) -> str:
    if count >= 10_000:
        text = f'{count / 10_000:.1f}'.rstrip('0').rstrip('.')
        return text + '만'
    return f'{count:,}'


def format_cost(cost: float | None, unit: str | None) -> str | None:
    if cost is None or unit is None:
        return None
    if unit == 'credits':
        return f'{cost:.0f} 크레딧' if cost >= 1 else '1 크레딧 미만'
    if unit == 'usd':
        return f'${cost:.2f}' if cost >= 0.01 else '$0.01 미만'
    return None


class _Row:
    __slots__ = ('ts', 'profile', 'account', 'family', 'model', 'tier', 'input', 'output', 'window', 'sub')

    def __init__(self, profile: str, data: dict):
        self.ts = float(data['ts'])
        self.profile = profile
        self.account = data.get('account') if isinstance(data.get('account'), str) else None
        self.family = data.get('family') if isinstance(data.get('family'), str) else None
        self.model = data.get('model') if isinstance(data.get('model'), str) else None
        self.tier = normalize_tier(data.get('tier'))
        self.input = int(data['input'])
        self.output = int(data.get('output') or 0)
        window = data.get('window')
        self.window = window if type(window) is int and window > 0 else None
        self.sub = data.get('sub') == 1


def _parse(profile: str, line: bytes) -> tuple[str, _Row] | None:
    try:
        data = json.loads(line)
    except (ValueError, UnicodeError, RecursionError):
        return None
    if not isinstance(data, dict) or data.get('v') != 1:
        return None
    key = data.get('thread')
    if not isinstance(key, str) or len(key) != 24:
        return None
    if type(data.get('input')) is not int or not isinstance(data.get('ts'), (int, float)):
        return None
    try:
        return key, _Row(profile, data)
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def _prefix_break(previous: int, rows: Iterable[_Row], family: str | None) -> str | None:
    """Why a prefix cached at ``previous`` input tokens broke over ``rows``."""
    for row in rows:
        if row.family != family:
            return 'provider_changed'
        if row.input < COMPACTION_DROP * previous:
            return 'compacted'
        previous = row.input
    return None


class _ThreadRows:
    """One thread's recent rows in time order, bounded, without losing anchors.

    Rows that leave the window keep the last row of each (profile, family) and
    (account, family) and the times of the newest family change and compaction
    among them, which is all an anchor older than the window needs.
    """

    __slots__ = ('rows', 'anchors', 'last_evicted', 'family_break', 'compaction_break', 'sub')

    def __init__(self):
        self.rows: list[_Row] = []
        self.anchors: dict[tuple, _Row] = {}
        self.last_evicted: _Row | None = None
        self.family_break: float | None = None
        self.compaction_break: float | None = None
        self.sub = False

    def add(self, row: _Row) -> None:
        self.sub = self.sub or row.sub
        if self.last_evicted is not None and row.ts < self.last_evicted.ts:
            # Arrived after newer rows already left the window: anchor only.
            self._remember(row)
            return
        rows = self.rows
        index = len(rows)
        while index and rows[index - 1].ts > row.ts:
            index -= 1
        rows.insert(index, row)
        while len(rows) > MAX_ROWS_PER_THREAD:
            old = rows.pop(0)
            previous = self.last_evicted
            if previous is not None:
                if old.family != previous.family:
                    self.family_break = old.ts
                elif old.input < COMPACTION_DROP * previous.input:
                    self.compaction_break = old.ts
            self._remember(old)
            self.last_evicted = old

    def _remember(self, row: _Row) -> None:
        keys = [('profile', row.profile, row.family)]
        if row.account is not None:
            keys.append(('account', row.account, row.family))
        for key in keys:
            known = self.anchors.get(key)
            if known is None or known.ts <= row.ts:
                self.anchors[key] = row
        while len(self.anchors) > MAX_ANCHORS_PER_THREAD:
            del self.anchors[min(self.anchors, key=lambda item: self.anchors[item].ts)]

    @property
    def newest(self) -> _Row:
        return self.rows[-1]

    def anchor(self, profile: dict) -> tuple[_Row | None, str | None]:
        """The newest row this profile's cache holds, and why it broke since (or None)."""
        family, account = profile['family'], profile['account']
        rows = self.rows
        for index in range(len(rows) - 1, -1, -1):
            row = rows[index]
            if row.family == family and (row.profile == profile['id'] or (account is not None and row.account == account)):
                return row, _prefix_break(row.input, rows[index + 1:], family)
        candidates = [self.anchors.get(('profile', profile['id'], family))]
        if account is not None:
            candidates.append(self.anchors.get(('account', account, family)))
        candidates = [row for row in candidates if row is not None]
        if not candidates:
            return None, None
        anchor = max(candidates, key=lambda row: row.ts)
        if self.family_break is not None and self.family_break > anchor.ts:
            return anchor, 'provider_changed'
        if self.compaction_break is not None and self.compaction_break > anchor.ts:
            return anchor, 'compacted'
        previous = anchor.input
        last = self.last_evicted
        if last is not None and last is not anchor and last.ts >= anchor.ts:
            if last.family != family:
                return anchor, 'provider_changed'
            previous = last.input
        return anchor, _prefix_break(previous, rows, family)

    def window(self, family: str | None) -> int | None:
        for row in reversed(self.rows):
            if row.family == family and row.window:
                return row.window
        known = [row for row in self.anchors.values() if row.family == family and row.window]
        return max(known, key=lambda row: row.ts).window if known else None


def _identity(info: os.stat_result) -> tuple:
    return info.st_ino, getattr(info, 'st_birthtime', None) or info.st_ctime


class LedgerIndex:
    """Incremental reader of every profile's ledger, bounded in memory."""

    def __init__(self, instances: Path | str, clock=time.time):
        self.instances = Path(instances)
        self.clock = clock
        self.cursors: dict[str, dict] = {}
        self.threads: dict[str, _ThreadRows] = {}
        self.latest: dict[str, _Row] = {}

    def refresh(self, profile_ids) -> None:
        batch: list[tuple[str, _Row]] = []
        for profile in profile_ids:
            try:
                self._read_profile(profile, batch)
            except OSError:
                continue
        # Profiles' files interleave in time: one sort per refresh, then rows
        # enter each thread in order.
        batch.sort(key=lambda item: item[1].ts)
        for key, row in batch:
            thread = self.threads.get(key)
            if thread is None:
                thread = self.threads[key] = _ThreadRows()
            thread.add(row)
            latest = self.latest.get(row.profile)
            if latest is None or row.ts >= latest.ts:
                self.latest[row.profile] = row
        self._prune()

    def _read_profile(self, profile: str, batch: list) -> None:
        directory = self.instances / profile
        path, old = directory / LEDGER_FILE, directory / LEDGER_OLD_FILE
        cursor = self.cursors.get(profile)
        try:
            info = path.stat()
        except FileNotFoundError:
            info = None
        identity = _identity(info) if info else None
        if cursor is None:
            offset = self._first_look(profile, path, old, info is not None, batch)
        elif info is None:
            offset = 0
        elif identity != cursor['identity'] or info.st_size < cursor['offset']:
            # Rotated: finish the previous file where we stopped, then start over.
            try:
                if _identity(old.stat()) == cursor['identity']:
                    self._read(profile, old, cursor['offset'], MAX_READ_BYTES, batch)
            except FileNotFoundError:
                pass
            offset = self._read(profile, path, 0, MAX_READ_BYTES, batch)
        elif info.st_size > cursor['offset']:
            offset = self._read(profile, path, cursor['offset'], MAX_READ_BYTES, batch)
        else:
            offset = cursor['offset']
        self.cursors[profile] = {'identity': identity, 'offset': offset}

    def _first_look(self, profile: str, path: Path, old: Path, exists: bool, batch: list) -> int:
        """Recent rows, newest first, until the report horizon or the byte budget."""
        horizon = self.clock() - REPORT_HORIZON_SECONDS
        end, used, covered = 0, 0, False
        if exists:
            end, used, covered = self._read_back(profile, path, FIRST_READ_BYTES, horizon, batch)
        if not covered and used < FIRST_READ_BYTES:
            # The whole current file is newer than the horizon (it was just
            # rotated): the rotated file holds the rest of the recent past.
            self._read_back(profile, old, FIRST_READ_BYTES - used, horizon, batch)
        return end

    def _read_back(self, profile: str, path: Path, budget: int, horizon: float, batch: list) -> tuple[int, int, bool]:
        """Read complete lines from the end backward. Returns (end offset, bytes read, covered).

        ``covered`` means the rows reach back past ``horizon`` or older data
        remains unread in this file, so an older file is not needed.
        """
        try:
            with open(path, 'rb') as stream:
                size = os.fstat(stream.fileno()).st_size
                position, data, reached = size, b'', False
                while position > 0 and size - position < budget:
                    step = min(FIRST_BLOCK_BYTES, position, budget - (size - position))
                    position -= step
                    stream.seek(position)
                    data = stream.read(step) + data
                    start = data.find(b'\n') + 1 if position > 0 else 0
                    stop = data.find(b'\n', start)
                    first = _parse(profile, data[start:stop]) if stop > start else None
                    if first is not None and first[1].ts < horizon:
                        reached = True
                        break
        except FileNotFoundError:
            return 0, 0, False
        end = data.rfind(b'\n') + 1
        lines = data[:end].split(b'\n')
        if position > 0 and lines:
            lines = lines[1:]  # The first line of a tail read may be partial.
        for line in lines:
            if line:
                parsed = _parse(profile, line)
                if parsed is not None:
                    batch.append(parsed)
        return position + end, size - position, reached or position > 0

    def _read(self, profile: str, path: Path, offset: int, limit: int, batch: list) -> int:
        """Consume complete lines from offset; a partial last line waits for the next read."""
        try:
            with open(path, 'rb') as stream:
                size = os.fstat(stream.fileno()).st_size
                stream.seek(offset)
                data = stream.read(min(limit, max(0, size - offset)))
        except FileNotFoundError:
            return 0
        end = data.rfind(b'\n') + 1
        for line in data[:end].split(b'\n'):
            if line:
                parsed = _parse(profile, line)
                if parsed is not None:
                    batch.append(parsed)
        return offset + end

    def _prune(self) -> None:
        horizon = self.clock() - ROW_HORIZON_SECONDS
        for key in [key for key, thread in self.threads.items() if thread.newest.ts < horizon]:
            del self.threads[key]
        if len(self.threads) > MAX_THREADS:
            keep = sorted(self.threads, key=lambda key: self.threads[key].newest.ts)[-MAX_THREADS:]
            self.threads = {key: self.threads[key] for key in keep}


def _weekly_remaining(profile: dict) -> float | None:
    windows = (profile.get('usage') or {}).get('windows') or []
    windows = [window for window in windows if isinstance(window, dict)]
    chosen = next((w for w in windows if (w.get('label') or w.get('name')) == '주간'), windows[0] if windows else None)
    if not chosen:
        return None
    value = chosen.get('remaining_percent')
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return max(0.0, min(100.0, float(value)))
    used = chosen.get('used_percent')
    if isinstance(used, (int, float)) and not isinstance(used, bool):
        return max(0.0, min(100.0, 100.0 - float(used)))
    return None


class CacheWarmth:
    """State-poll facade: refresh the index, then describe recent threads.

    With ``background`` the first read of each profile's ledger runs on a
    worker thread; until it finishes the poll returns the previous summary
    (empty at first, ``loading``) instead of waiting for it.
    """

    def __init__(self, instances: Path | str, prices: Path | str | None = None, clock=time.time,
                 background: bool = False):
        self.index = LedgerIndex(instances, clock)
        self.prices_path = Path(prices) if prices is not None else None
        self.clock = clock
        self.lock = threading.Lock()
        self.background = background
        self.builder: threading.Thread | None = None
        self._builder_lock = threading.Lock()
        # Profiles handed to a background first read. One that failed there
        # is read inline by a later refresh instead of restarting the worker.
        self._attempted: set[str] = set()
        self._last: dict = {'version': 1, 'threads': {}}
        self._kinds = KINDS
        self._prices_checked = 0.0

    def kinds(self) -> dict[str, dict]:
        """Built-in table, with optional per-kind overrides from cache-prices.json."""
        now = self.clock()
        if self.prices_path is None or now - self._prices_checked < 60:
            return self._kinds
        self._prices_checked = now
        kinds = {name: dict(value) for name, value in KINDS.items()}
        try:
            if self.prices_path.is_file() and self.prices_path.stat().st_size <= 65536:
                data = json.loads(self.prices_path.read_text(encoding='utf-8'))
                for name, override in (data.items() if isinstance(data, dict) else ()):
                    if name not in kinds or not isinstance(override, dict):
                        continue
                    for field in ('input', 'cached', 'output', 'write'):
                        value = override.get(field)
                        if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 1e6:
                            kinds[name][field] = float(value)
                    if override.get('unit') in ('credits', 'usd'):
                        kinds[name]['unit'] = override['unit']
                        kinds[name]['assumed'] = False
                    minutes = override.get('warm_minutes')
                    if isinstance(minutes, (int, float)) and not isinstance(minutes, bool) and 1 <= minutes <= 7 * 24 * 60:
                        kinds[name]['warm_seconds'] = float(minutes) * 60
        except (OSError, ValueError, UnicodeError):
            pass
        self._kinds = kinds
        return kinds

    def wait(self, timeout: float | None = None) -> None:
        """Wait for a background first read (tests and diagnostics)."""
        builder = self.builder
        if builder is not None:
            builder.join(timeout)

    def _build(self, profile_ids: list[str]) -> None:
        with self.lock:
            try:
                self.index.refresh(profile_ids)
            except (OSError, ValueError, TypeError, KeyError, AttributeError):
                pass

    def summary(self, profiles: list[dict], families: dict[str, tuple[str | None, str | None]] | None = None,
                pinned: Iterable[str] = ()) -> dict:
        """``profiles``: state profiles; ``families``: profile id -> (family, model) for API profiles;
        ``pinned``: thread ids always described (opened or selected tasks), beside the recent ones."""
        candidates = [p for p in profiles if isinstance(p, dict) and isinstance(p.get('id'), str)]
        ids = [p['id'] for p in candidates]
        if self.background:
            with self._builder_lock:
                if self.builder is not None and self.builder.is_alive():
                    return dict(self._last, loading=True)
                unread = [profile for profile in ids if profile not in self._attempted]
                if unread:
                    self._attempted.update(unread)
                    self.builder = threading.Thread(target=self._build, args=(unread,), daemon=True,
                                                    name='codex-cache-warmth-index')
                    self.builder.start()
                    return dict(self._last, loading=True)
            if not self.lock.acquire(blocking=False):
                return dict(self._last)  # Another poll is refreshing right now.
        else:
            self.lock.acquire()
        try:
            self.index.refresh(ids)
            now = self.clock()
            kinds = self.kinds()
            described = [self._profile(p, (families or {}).get(p['id'])) for p in candidates]
            threads = self.index.threads
            keys = []
            for thread_id in pinned:
                if isinstance(thread_id, str) and thread_id and len(keys) < MAX_PINNED_THREADS:
                    key = thread_hash(thread_id)
                    if key in threads and key not in keys:
                        keys.append(key)
            # Sub-agent threads are never a task a card shows; they do not
            # push the user's tasks out of the recent set.
            recent = sorted((key for key, thread in threads.items()
                             if not thread.sub and now - thread.newest.ts <= REPORT_HORIZON_SECONDS),
                            key=lambda key: threads[key].newest.ts, reverse=True)
            for key in recent[:MAX_REPORTED_THREADS]:
                if key not in keys:
                    keys.append(key)
            described_threads = {key: self._thread(threads[key], described, now, kinds) for key in keys}
            self._last = {'version': 1, 'threads': {key: value for key, value in described_threads.items() if value}}
            return self._last
        finally:
            self.lock.release()

    def thread(self, thread_id: str, profiles: list[dict], families=None) -> dict | None:
        """One thread by id (tests and diagnostics)."""
        summary = self.summary(profiles, families, pinned=[thread_id])
        return summary['threads'].get(thread_hash(thread_id))

    def _profile(self, profile: dict, family: tuple | None) -> dict:
        latest = self.index.latest.get(profile['id'])
        if profile.get('auth_mode') == 'external':
            provider_family, model = family or (None, None)
            provider_family = provider_family or (latest.family if latest else None)
            model = model or (latest.model if latest else None)
            account = ('provider:' + provider_family) if provider_family else None
        else:
            provider_family, model = OPENAI_FAMILY, (latest.model if latest else None)
            fingerprint = profile.get('account_fingerprint')
            account = fingerprint[:12].lower() if isinstance(fingerprint, str) and len(fingerprint) >= 12 else None
        return {'id': profile['id'], 'alias': str(profile.get('alias') or profile['id'][:8]),
                'family': provider_family, 'model': model, 'account': account,
                'tier': latest.tier if latest else None, 'remaining': _weekly_remaining(profile)}

    def _thread(self, thread: _ThreadRows, profiles: list[dict], now: float, kinds: dict) -> dict | None:
        if not thread.rows:
            return None
        latest = thread.newest
        context = latest.input + latest.output
        entries: dict[str, dict] = {}
        for profile in profiles:
            if profile['family'] is None:
                continue  # An API profile whose provider is not known yet.
            entries[profile['id']] = self._estimate(profile, thread, context, latest, now, kinds)
        for profile in profiles:
            entry = entries.get(profile['id'])
            if entry is None or entry['state'] == 'warm':
                continue
            warm = [(entries[other['id']], other) for other in profiles
                    if other['id'] != profile['id'] and other['family'] == profile['family']
                    and entries.get(other['id'], {}).get('state') == 'warm']
            if warm:
                entry['tone'] = 'warning'
                best, owner = max(warm, key=lambda item: (item[0]['warm'], -item[0]['age']))
                if context >= NOTICE_MIN_CONTEXT:
                    minutes = max(1, round(best['age'] / 60))
                    price = entry['cost_text'] or f"{korean_tokens(entry['uncached'])} token"
                    if entry.get('assumed_price'):
                        price += ', ' + ASSUMED_PRICE
                    text = (f"{owner['alias']}에서 {korean_tokens(best['warm'])} token이 {minutes}분 전에 캐시됐습니다. "
                            f"여기서 이어가면 첫 요청이 캐시 없이 처리됩니다(약 {price}).")
                    if owner['remaining'] is not None:
                        text += f" {owner['alias']} 남은 사용량 {owner['remaining']:.0f}%."
                    entry['notice'] = text
                    entry['warm_profile'] = owner['id']
        for entry in entries.values():
            # Internal to the notice choice; the 4 s state poll carries less.
            for name in ('age', 'tier', 'cost_text'):
                entry.pop(name, None)
        return {'context': context, 'updated': round(latest.ts, 3), 'profiles': entries}

    def _estimate(self, profile: dict, thread: _ThreadRows, context: int, latest: _Row, now: float, kinds: dict) -> dict:
        anchor, broken = thread.anchor(profile)
        kind_name = kind_of(profile['family'], (anchor.model if anchor else None) or profile['model'])
        kind = kinds[kind_name]
        state, reason, warm, age, left = 'cold', 'not_served', 0, 0.0, None
        if anchor is not None:
            age = max(0.0, now - anchor.ts)
            if broken is not None:
                reason = broken
            else:
                share = retention(kind, age)
                warm = int(anchor.input * share)
                if age < kind['warm_seconds']:
                    state, reason, left = 'warm', None, max(1, math.ceil((kind['warm_seconds'] - age) / 60))
                elif share > 0:
                    state, reason = 'partial', None
                else:
                    reason = 'expired'
        target = context
        if latest.family != profile['family']:
            # A family change rebuilds the context within the target's budget
            # (about 60% of its window for GPT); the exact size is unknown.
            window = (anchor.window if anchor else None) or thread.window(profile['family']) or kind.get('window')
            if window:
                target = min(target, int(window * REBUILD_SHARE))
        warm = min(warm, target)
        uncached = max(0, target - warm)
        # The tier the thread last ran with on this cache; the profile's newest
        # tier only when this profile never served the thread.
        tier = anchor.tier if anchor is not None else normalize_tier(profile['tier'])
        cost = None
        if kind.get('unit') and 'input' in kind:
            multiplier = kind['tiers'].get(tier or '', 1.0)
            cost = (uncached * kind['input'] * kind['write'] + warm * kind['cached']) / 1_000_000 * multiplier
        cost_text = format_cost(cost, kind.get('unit'))
        assumed = bool(kind.get('assumed') and cost_text)
        if state in ('warm', 'partial'):
            if cost_text:
                detail = f"첫 요청 ≈ {cost_text}" + (f" ({ASSUMED_PRICE})" if assumed else '')
            else:
                detail = f"캐시 ≈ {korean_tokens(warm)} token"
            line = (f"이 작업 캐시 · 약 {left}분 남음 · {detail}" if state == 'warm'
                    else f"이 작업 캐시 일부 · {detail}")
        elif cost_text:
            suffix = f", {ASSUMED_PRICE}" if assumed else ''
            line = f"이 작업 캐시 없음 · 첫 요청 ≈ {cost_text} ({korean_tokens(uncached)} token{suffix})"
        else:
            line = f"이 작업 캐시 없음 · 첫 요청 ≈ {korean_tokens(uncached)} token"
        entry = {'state': state, 'reason': reason, 'kind': kind_name, 'warm': warm, 'uncached': uncached,
                 'age': round(age, 1), 'minutes_left': left, 'cost': None if cost is None else round(cost, 4),
                 'unit': kind.get('unit'), 'tier': tier, 'cost_text': cost_text,
                 'line': line, 'tone': 'ready' if state == 'warm' else 'muted'}
        if assumed:
            entry['assumed_price'] = True
        return entry
