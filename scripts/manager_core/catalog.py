"""Read-only metadata index. This alone is not original GUI federation."""
import json
from pathlib import Path
import sqlite3
from datetime import datetime, timezone
from contextlib import closing
from uuid import UUID


def list_source(source, limit=4096, *, strict=False):
    home = Path(source['home'])
    rows = []
    # SQLite connections are read-only and bounded; never migrate or open a writer.
    candidates = sorted(set([*home.glob('state_*.sqlite'), *(home/'sqlite').glob('state_*.sqlite')]))
    for path in candidates:
        try:
            with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True, timeout=.4)) as db:
                db.execute('PRAGMA query_only=ON')
                columns = {r[1] for r in db.execute('PRAGMA table_info(threads)')}
                if 'id' not in columns or not {'name','title','preview','first_user_message'}.intersection(columns):
                    continue
                select = ['id'] + [c for c in ('name','title','preview','first_user_message','cwd','updated_at','archived','archived_at','rollout_path') if c in columns]
                order = ' ORDER BY updated_at DESC' if 'updated_at' in columns else ''
                query = 'SELECT '+','.join(select)+' FROM threads'+order
                cursor = db.execute(query if limit is None else query+' LIMIT ?', () if limit is None else (limit,))
                for row in cursor:
                    item = dict(zip(select, row))
                    try:thread_id=str(UUID(item['id']))
                    except (ValueError,TypeError,AttributeError):continue
                    title=next((value.strip() for field in ('name','title','preview','first_user_message')
                                if isinstance(value:=item.get(field),str) and value.strip()),thread_id)
                    rows.append(dict(thread_id=thread_id, title=title[:512], cwd=str(item['cwd'])[:4096] if item.get('cwd') is not None else None,
                                     updated_at=item.get('updated_at'), archived=bool(item.get('archived') or item.get('archived_at')),
                                     rollout_path=item.get('rollout_path'),
                                     host_id=source['host_id'], source_store_id=source['id'],
                                     source_alias=source['alias'], source_home=str(home)))
        except (sqlite3.Error, OSError):
            if strict:
                raise
            continue
    # Some newer stores publish a compact JSONL index. Read only known fields.
    index = home/'session_index.jsonl'
    if not rows and index.is_file():
        with index.open(encoding='utf-8') as file:
            for line in file:
                try:
                    item=json.loads(line)
                    tid=item.get('id') or item.get('thread_id')
                    if tid:
                        tid=str(UUID(tid))
                        rows.append(dict(thread_id=tid,title=str(item.get('thread_name') or item.get('title') or tid)[:512],
                                         updated_at=item.get('updated_at'),host_id=source['host_id'],
                                         source_store_id=source['id'],source_alias=source['alias'],source_home=str(home)))
                except (ValueError, TypeError, AttributeError):
                    if strict:
                        raise
                    continue
        if limit is not None:
            rows=rows[-limit:]
    return rows


def sort_conversations(rows):
    def timestamp(row):
        value = row.get('updated_at')
        try:
            return float(value)
        except (ValueError, TypeError):
            try:
                parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
                return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()
            except (ValueError, TypeError, AttributeError, OverflowError):
                return 0
    return sorted(rows, key=timestamp, reverse=True)


def list_catalog(sources, limit=4096):
    sources = [s for s in sources if not s.get('history_migrated_to')]
    output=[]
    errors=[]
    for source in sources:
        if source.get('host_id') != 'local':
            continue
        try:
            output.extend(list_source(source, limit))
        except OSError:
            errors.append(dict(source_store_id=source['id'],message='출처를 읽지 못했습니다.'))
    canonical=[(Path(s['home']).resolve(),s) for s in sources if s.get('host_id')=='local']
    for row in output:
        rollout=row.pop('rollout_path',None)
        if not rollout:continue
        try:
            path=Path(rollout).resolve(strict=True)
            matches=[s for home,s in canonical if any(path.is_relative_to(home/name) for name in ('sessions','archived_sessions'))]
            if len(matches)==1:
                source=matches[0]
                row.update(source_store_id=source['id'],source_alias=source['alias'],source_home=source['home'])
        except (OSError,ValueError):continue
    unique={}
    for row in sort_conversations(output):
        unique.setdefault((row['host_id'],row['source_store_id'],row['thread_id']),row)
    conversations=sort_conversations(unique.values())
    return dict(conversations=conversations,errors=errors,source_limit=limit,
                possibly_truncated=limit is not None and len(output)>=limit,original_gui_federation=False)
