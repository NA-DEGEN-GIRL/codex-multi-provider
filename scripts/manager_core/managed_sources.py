"""Per-instance source allowlists from durable source ownership records."""
import json
from pathlib import Path
from .authority import read,_authority_guard
from .store import atomic_json


def mark(profile):
    home=Path(profile['home']).resolve()
    expected={'host_id':'local','store_id':'manager:'+profile['id']}
    file=home/'managed-source.json'
    if file.exists():
        if json.loads(file.read_text(encoding='utf-8'))!=expected:
            raise RuntimeError('관리 저장소 식별자가 일치하지 않습니다.')
    else:atomic_json(file,expected)


def manifest(store,profile):
    parent=store.directory/'profiles'/profile['id']
    if parent.resolve(strict=True)!=parent:
        raise RuntimeError('관리 프로필의 실제 경로가 변경되었습니다.')
    with _authority_guard(parent/'managed-sources.guard'):
        return _manifest(store,profile,parent/'managed-sources.json')


def _manifest(store,profile,file):
    sources=[];bindings={};homes={}
    for candidate in store.read()['profiles']:
        if candidate.get('view_only'):continue
        home=Path(candidate['home']).resolve();marker=home/'managed-source.json'
        if not marker.is_file():continue
        if home!=store.directory/'profiles'/candidate['id']/'codex':
            raise RuntimeError('관리 저장소의 실제 경로가 변경되었습니다.')
        expected={'host_id':'local','store_id':'manager:'+candidate['id']}
        if json.loads(marker.read_text(encoding='utf-8'))!=expected:
            raise RuntimeError('관리 저장소 식별자가 일치하지 않습니다.')
        sources.append(dict(hostId='local',sourceStoreId=expected['store_id'],codexHome=str(home)))
        homes[expected['store_id']]=home
        for authority in (home/'managed-authority').glob('*.json'):
            data=read(home,authority.stem)
            if data['owner_profile_id']==profile['id']:
                if data['thread_id'] in bindings:
                    raise RuntimeError('서로 다른 저장소에 같은 대화 ID가 있습니다.')
                bindings[data['thread_id']]=dict(threadId=data['thread_id'],hostId='local',sourceStoreId=data['store_id'],
                                               ownerProfileId=profile['id'],ownershipEpoch=data['epoch'],recordRevision=data['revision'])
    if file.exists():
        if file.is_symlink() or file.stat().st_size>4*1024*1024:
            raise RuntimeError('관리 저장소 연결 파일의 크기 또는 경로가 올바르지 않습니다.')
        existing=json.loads(file.read_text(encoding='utf-8'))
        if existing.get('version')!=1 or existing.get('profileId')!=profile['id'] or existing.get('hostId')!='local':
            raise RuntimeError('관리 저장소 연결 파일의 프로필이 일치하지 않습니다.')
        for item in existing.get('bindings',[]):
            home=homes.get(item.get('sourceStoreId'))
            if home is None:continue
            try:data=read(home,item['threadId'])
            except (OSError,ValueError,KeyError):continue
            current=dict(threadId=data['thread_id'],hostId=data['host_id'],sourceStoreId=data['store_id'],
                         ownerProfileId=data['owner_profile_id'],ownershipEpoch=data['epoch'],recordRevision=data['revision'])
            if item!=current:continue
            if item['threadId'] in bindings and bindings[item['threadId']]['sourceStoreId']!=item['sourceStoreId']:
                raise RuntimeError('서로 다른 저장소에 같은 대화 ID가 있습니다.')
            bindings[item['threadId']]=current
    value=dict(version=1,profileId=profile['id'],hostId='local',sources=sources,
               bindings=sorted(bindings.values(),key=lambda item:item['threadId']))
    if len(sources)>256 or len(bindings)>4096 or len(json.dumps(value,ensure_ascii=False).encode('utf-8'))>4*1024*1024:
        raise RuntimeError('관리 저장소 연결 목록이 런타임 지원 범위를 초과했습니다.')
    atomic_json(file,value)
    return file
