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
        chosen = second if isinstance(second, dict) else first
    elif not isinstance(second, dict) or not second.get('windows'):
        chosen = first
    else:
        chosen = second if observed_timestamp(second) > observed_timestamp(first) else first
    other = first if chosen is second else second
    # A reply that only carried quota windows must not erase an earlier
    # reset-credit count; the credit snapshot has no window of its own.
    if (isinstance(chosen, dict) and isinstance(other, dict)
            and not chosen.get('reset_credits') and other.get('reset_credits')):
        chosen = {**chosen, 'reset_credits': deepcopy(other['reset_credits'])}
    return chosen


def _reset_credits(reply):
    """Return the opaque reset-credit count without keeping credit identity."""
    summary = reply.get('rateLimitResetCredits')
    if not isinstance(summary, dict):
        return None
    count = summary.get('availableCount')
    if type(count) is not int or count < 0 or count > 1_000_000:
        return None
    expires = None
    credits = summary.get('credits')
    if isinstance(credits, list):
        values = [item.get('expiresAt') for item in credits
                  if isinstance(item, dict) and item.get('status') == 'available'
                  and type(item.get('expiresAt')) is int]
        if values:
            expires = min(values)
    return dict(available=count, expires_at=expires)


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
                freshness='live' if windows else 'unknown', error=None,
                reset_credits=_reset_credits(reply))
