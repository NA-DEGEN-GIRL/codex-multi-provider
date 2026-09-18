"""Normalize quota replies without retaining identity, tokens, or server error text."""
from datetime import datetime, timezone
from copy import deepcopy
import math


def observed_timestamp(usage):
    try:
        value = datetime.fromisoformat(usage.get('observed_at', '').replace('Z', '+00:00'))
        return value.timestamp() if value.tzinfo else 0
    except (ValueError, TypeError, AttributeError):
        return 0


def newer(first, second):
    """An older imported cache must not replace a newer verified quota reply."""
    if not isinstance(first, dict) or not first.get('windows'):
        return second if isinstance(second, dict) else first
    if not isinstance(second, dict) or not second.get('windows'):
        return first
    return second if observed_timestamp(second) > observed_timestamp(first) else first


def presentation(usage, *, refreshing=False):
    result = deepcopy(usage) if isinstance(usage, dict) else dict(windows=[])
    age = max(0, datetime.now(timezone.utc).timestamp() - observed_timestamp(result))
    result['age_seconds'] = int(age) if observed_timestamp(result) else None
    result['refreshing'] = refreshing
    if not result.get('windows'):
        result['freshness'] = 'unknown'
    elif age > 120:
        result['freshness'] = 'stale'
    return result


def normalize(reply):
    buckets = reply.get('rateLimitsByLimitId')
    bucket = buckets.get('codex') if isinstance(buckets, dict) else None
    if not isinstance(bucket, dict):
        bucket = reply.get('rateLimits')
    windows = []
    if isinstance(bucket, dict):
        for key in ('primary', 'secondary'):
            value = bucket.get(key)
            if not isinstance(value, dict):
                continue
            used, duration = value.get('usedPercent'), value.get('windowDurationMins')
            if type(used) not in (int, float) or not math.isfinite(used):
                continue
            label = '5시간' if duration == 300 else '주간' if duration == 10080 else ('기본' if key == 'primary' else '추가')
            reset = value.get('resetsAt')
            windows.append(dict(label=label, used_percent=max(0, min(100, used)),
                                remaining_percent=max(0, min(100, 100-used)),
                                resets_at=reset if type(reset) is int else None))
    return dict(windows=windows, observed_at=datetime.now(timezone.utc).isoformat(),
                freshness='live' if windows else 'unknown', error=None)
