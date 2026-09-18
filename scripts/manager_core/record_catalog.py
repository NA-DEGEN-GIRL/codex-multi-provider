"""Build explicit native-viewer projections without loading conversation bodies."""
from collections import Counter
from functools import lru_cache
import hashlib
import json
from pathlib import Path
from uuid import UUID, uuid5
from .store import atomic_json

PROJECTION_NAMESPACE=UUID('14a0f21b-b529-45fc-bd9e-b07637424fa3')


def metadata(path):
    stat=path.stat()
    return _metadata(str(path),stat.st_size,stat.st_mtime_ns)


@lru_cache(maxsize=8192)
def _metadata(filename,size,modified):
    # Identity metadata occurs at the start; never load a whole conversation here.
    with Path(filename).open('rb') as stream:
        for _ in range(8):
            line=stream.readline(1024*1024+1)
            if not line or len(line)>1024*1024:return None
            try:item=json.loads(line)
            except (ValueError,UnicodeError):continue
            if item.get('type')=='session_meta':
                payload=item.get('payload',{})
                try:thread=str(UUID(payload['id']))
                except (ValueError,KeyError,TypeError):return None
                return {'thread_id':thread,'cwd':payload.get('cwd'), 'title':payload.get('title'),
                        'history_mode':payload.get('history_mode') or payload.get('historyMode')}
    return None


def build(root,sources,max_entries=4096,*,include_paginated=False):
    sources = [s for s in sources if not s.get('history_migrated_to')]
    root=Path(root).resolve()
    entries=[];mapping=[];issues=Counter();seen=set();physical_stores={}
    registered={str(Path(s['home']).resolve()).casefold():s for s in sources if s.get('host_id')=='local'}
    for source in sources:
        if source.get('host_id')!='local':continue
        home=Path(source['home'])
        for directory_name in ('sessions','archived_sessions'):
            directory=home/directory_name
            if not directory.is_dir():continue
            actual=directory.resolve()
            # llm-usage can link sessions to an already registered canonical store.
            canonical_home=actual.parent
            canonical_source=registered.get(str(canonical_home).casefold())
            if not canonical_source or actual.name!=directory_name:
                issues['unregistered_link_target']+=1;continue
            sid=canonical_source['id']
            prior=physical_stores.setdefault(str(canonical_home).casefold(),sid)
            if prior!=sid:
                issues['ambiguous_store']+=1;continue
            for candidate in directory.rglob('*.jsonl'):
                try:
                    path=candidate.resolve(strict=True)
                    if not path.is_relative_to(actual):
                        issues['outside_source']+=1;continue
                    physical=str(path).casefold()
                    if physical in seen:continue
                    seen.add(physical)
                    info=metadata(path)
                    if not info:
                        issues['invalid_metadata']+=1;continue
                    if info['history_mode'] not in (None,'legacy') and not (include_paginated and info['history_mode']=='paginated'):
                        issues['modern_history_unsupported']+=1;continue
                    if len(entries)>=max_entries:
                        issues['entry_limit']+=1;continue
                    key='local\0'+sid+'\0'+info['thread_id']
                    projection=str(uuid5(PROJECTION_NAMESPACE,key))
                    entry=dict(projectionThreadId=projection,threadId=info['thread_id'],hostId='local',
                               sourceStoreId=sid,codexHome=str(canonical_home),rolloutPath=str(path))
                    entries.append(entry)
                    mapping.append(dict(thread_id=info['thread_id'],projection_thread_id=projection,
                                        host_id='local',source_store_id=sid,source_alias=canonical_source['alias'],
                                        title=info['title'] or info['thread_id'],cwd=info['cwd']))
                except (OSError,ValueError):issues['unreadable']+=1
    # Two paths claiming one identity must not silently select an arbitrary copy.
    counts=Counter((e['hostId'],e['sourceStoreId'],e['threadId']) for e in entries)
    duplicate={key for key,count in counts.items() if count>1}
    if duplicate:
        issues['duplicate_identity']+=len(duplicate)
        entries=[e for e in entries if (e['hostId'],e['sourceStoreId'],e['threadId']) not in duplicate]
        mapping=[e for e in mapping if (e['host_id'],e['source_store_id'],e['thread_id']) not in duplicate]
    entries.sort(key=lambda e:e['projectionThreadId'])
    encoded=json.dumps(entries,ensure_ascii=False,sort_keys=True).encode('utf-8')
    manifest=dict(version=1,revision=hashlib.sha256(encoded).hexdigest(),hostId='local',entries=entries)
    if len(json.dumps(manifest,ensure_ascii=False).encode('utf-8'))>4*1024*1024:
        raise RuntimeError('전체 기록 색인이 현재 런타임 한도를 초과했습니다. 일부만 완료로 표시하지 않습니다.')
    path=root/'work/control-center/catalog/local-records.json'
    if not path.exists() or json.loads(path.read_text(encoding='utf-8')).get('revision')!=manifest['revision']:
        atomic_json(path,manifest)
    return dict(path=str(path),revision=manifest['revision'],entries=len(entries),mapping=mapping,
                limitations=dict(issues),complete=not issues)
