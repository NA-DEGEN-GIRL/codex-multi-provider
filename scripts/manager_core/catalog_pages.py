"""Search the complete metadata view; return bounded pages to the Windows shell."""
import base64
from bisect import bisect_right
from datetime import datetime, timezone
import hashlib
import json
import math


def _time(value):
    try:
        number = float(value)
    except (ValueError, TypeError):
        try:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
            number = (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()
        except (ValueError, TypeError, AttributeError, OverflowError):
            return 0.0
    return number if math.isfinite(number) else 0.0


def _key(row):
    return (-_time(row.get('updated_at')), row['host_id'], row['source_store_id'], row['thread_id'])


def legacy_view(value):
    """Keep older shells usable without an oversized single IPC response."""
    selected, size = [], len(json.dumps({k: v for k, v in value.items() if k != 'conversations'}, ensure_ascii=False).encode())
    for row in value['conversations']:
        size += len(json.dumps(row, ensure_ascii=False).encode()) + 2
        if size > 7 * 1024 * 1024:
            break
        selected.append(row)
    return {**value, 'conversations': selected,
            'possibly_truncated': value.get('possibly_truncated', False) or len(selected) < len(value['conversations'])}


def page(value, args, scope):
    limit, query, thread_id = args.get('limit', 200), args.get('query', ''), args.get('thread_id')
    if (type(limit) is not int or not 1 <= limit <= 500 or not isinstance(query, str) or len(query) > 512
            or thread_id is not None and (not isinstance(thread_id, str) or len(thread_id) > 64)):
        raise ValueError('Invalid catalog search')
    query = query.strip().casefold()
    digest = hashlib.sha256(json.dumps([scope, query, thread_id], sort_keys=True).encode()).hexdigest()
    after = None
    cursor = args.get('cursor')
    if cursor is not None:
        try:
            if not isinstance(cursor, str) or len(cursor) > 4096:
                raise ValueError('Invalid catalog cursor')
            decoded = json.loads(base64.b64decode(cursor, altchars=b'-_', validate=True))
            after = decoded['after']
            if (decoded.get('scope') != digest or not isinstance(after, list) or len(after) != 4
                    or type(after[0]) not in (int, float) or not math.isfinite(after[0])
                    or any(not isinstance(v, str) for v in after[1:])):
                raise ValueError('Invalid catalog cursor')
            after = tuple(after)
        except (ValueError, TypeError, KeyError, UnicodeError) as error:
            raise ValueError('대화 목록의 검색 범위가 바뀌었습니다. 첫 페이지에서 다시 확인하세요.') from error
    rows = [r for r in value['conversations'] if (thread_id is None or r['thread_id'] == thread_id)
            and (not query or query in '\n'.join(str(r.get(k) or '') for k in
                 ('title', 'thread_id', 'host_id', 'source_alias', 'source_store_id', 'cwd')).casefold())]
    rows.sort(key=_key)
    start = bisect_right(rows, after, key=_key) if after is not None else 0
    selected = []
    size = len(json.dumps({k: v for k, v in value.items() if k != 'conversations'}, ensure_ascii=False).encode())
    for row in rows[start:start + limit]:
        size += len(json.dumps(row, ensure_ascii=False).encode()) + 2
        if size > 7 * 1024 * 1024:
            if not selected:
                raise ValueError('Catalog entry exceeds the picker response size limit')
            break
        selected.append(row)
    next_cursor = None
    if start + len(selected) < len(rows):
        next_cursor = base64.urlsafe_b64encode(json.dumps(dict(scope=digest, after=_key(selected[-1]))).encode()).decode()
    return {**value, 'conversations': selected, 'total_count': len(value['conversations']),
            'matching_count': len(rows), 'next_cursor': next_cursor, 'has_more': next_cursor is not None}
