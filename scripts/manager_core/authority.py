"""Durable compare-and-swap of a managed conversation's execution owner.

Only the orchestration layer may call transfer after proven source shutdown.
This module never guesses that a lock expired and never starts a model turn.
"""
from contextlib import contextmanager,ExitStack
import json
import os
from pathlib import Path
import tempfile
from uuid import UUID

MAX_COUNTER=2**53-1
FIELDS={'version','host_id','store_id','thread_id','owner_profile_id','epoch','revision'}


@contextmanager
def _authority_guard(path, *, exclusive=True):
    """Lock the same permanent file used by Rust's managed write permits."""
    path=Path(path)
    if path.is_symlink():
        raise ValueError('실행 소유 잠금 파일이 외부 경로에 연결돼 있습니다.')
    flags=os.O_RDWR|os.O_CREAT|getattr(os,'O_BINARY',0)|getattr(os,'O_NOFOLLOW',0)
    fd=os.open(path,flags,0o600)
    locked=False
    try:
        if os.fstat(fd).st_nlink!=1:
            raise ValueError('실행 소유 잠금 파일을 공유 경로로 사용할 수 없습니다.')
        if os.name=='nt':
            import ctypes
            from ctypes import wintypes
            import msvcrt
            class Overlapped(ctypes.Structure):
                _fields_=[('Internal',ctypes.c_size_t),('InternalHigh',ctypes.c_size_t),
                          ('Offset',wintypes.DWORD),('OffsetHigh',wintypes.DWORD),('hEvent',wintypes.HANDLE)]
            kernel=ctypes.WinDLL('kernel32',use_last_error=True)
            kernel.LockFileEx.argtypes=[wintypes.HANDLE,wintypes.DWORD,wintypes.DWORD,
                                       wintypes.DWORD,wintypes.DWORD,ctypes.POINTER(Overlapped)]
            kernel.LockFileEx.restype=wintypes.BOOL
            kernel.UnlockFileEx.argtypes=[wintypes.HANDLE,wintypes.DWORD,wintypes.DWORD,
                                         wintypes.DWORD,ctypes.POINTER(Overlapped)]
            kernel.UnlockFileEx.restype=wintypes.BOOL
            handle=msvcrt.get_osfhandle(fd);overlapped=Overlapped()
            if not kernel.LockFileEx(handle,1|(2 if exclusive else 0),0,0xffffffff,0xffffffff,ctypes.byref(overlapped)):
                error=ctypes.get_last_error()
                if error in (32,33,997):
                    raise RuntimeError('이 대화의 기록 쓰기가 진행 중입니다. 완료된 뒤 인계하세요.')
                raise OSError(error,'실행 소유 잠금을 확인하지 못했습니다.')
            locked=True
        else:
            import fcntl
            try:fcntl.flock(fd,(fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)|fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError('이 대화의 기록 쓰기가 진행 중입니다. 완료된 뒤 인계하세요.') from None
            locked=True
        yield
    finally:
        if locked:
            if os.name=='nt':kernel.UnlockFileEx(handle,0,0xffffffff,0xffffffff,ctypes.byref(overlapped))
            else:fcntl.flock(fd,fcntl.LOCK_UN)
        os.close(fd)


@contextmanager
def _writer_guard(home,thread_id):
    """Hold the canonical recorder lock so a stale process cannot reopen it."""
    home=Path(home).resolve(strict=True)
    directory=home/'thread-writer-locks'
    if directory.exists() and (directory.is_symlink() or directory.resolve()!=directory):
        raise ValueError('대화 저장 잠금 폴더가 외부 경로에 연결돼 있습니다.')
    directory.mkdir(exist_ok=True)
    with ExitStack() as held:
        with _authority_guard(directory/'.coordination.lock'):
            held.enter_context(_authority_guard(directory/(str(UUID(thread_id))+'.lock')))
        # Leave the file in place. Rust may clean it under coordination later.
        yield


def validate(value):
    if not isinstance(value,dict) or set(value)!=FIELDS or value['version']!=1:
        raise ValueError('실행 소유 기록의 형식이 올바르지 않습니다.')
    for field in ('thread_id','owner_profile_id'):
        if str(UUID(value[field]))!=value[field]:raise ValueError('실행 소유 기록의 ID가 올바르지 않습니다.')
    if not value['store_id'].startswith('manager:'):
        raise ValueError('관리 앱의 기록 저장소만 실행을 인계할 수 있습니다.')
    UUID(value['store_id'].removeprefix('manager:'))
    if not isinstance(value['host_id'],str) or not value['host_id']:
        raise ValueError('호스트가 없습니다.')
    for field in ('epoch','revision'):
        if type(value[field]) is not int or not 1<=value[field]<=MAX_COUNTER:
            raise ValueError('실행 소유 기록의 버전이 올바르지 않습니다.')
    return value


def _paths(home,thread_id):
    home=Path(home).resolve(strict=True);thread_id=str(UUID(thread_id))
    marker=home/'managed-source.json'
    declared=json.loads(marker.read_text(encoding='utf-8'))
    if declared.get('host_id')!='local' or not str(declared.get('store_id','')).startswith('manager:'):
        raise ValueError('이 호스트에서 관리하는 원본 저장소가 아닙니다.')
    directory=home/'managed-authority'
    if directory.exists() and (directory.is_symlink() or directory.resolve()!=directory):
        raise ValueError('실행 소유 폴더가 원본 저장소 밖으로 연결돼 있습니다.')
    return directory,directory/(thread_id+'.json'),declared


def read(home,thread_id):
    _,path,declared=_paths(home,thread_id)
    if path.is_symlink() or path.stat().st_size>4096:
        raise ValueError('실행 소유 파일을 확인할 수 없습니다.')
    data=validate(json.loads(path.read_text(encoding='utf-8')))
    if data['thread_id']!=thread_id or data['host_id']!=declared['host_id'] or data['store_id']!=declared['store_id']:
        raise ValueError('실행 소유 기록의 출처가 일치하지 않습니다.')
    return data


def transfer(home,expected,target_profile_id,*,release_verified):
    expected=validate(dict(expected));target_profile_id=str(UUID(target_profile_id))
    if release_verified is not True:
        raise RuntimeError('기존 실행 종료·기록 확정을 확인하기 전에는 인계하지 않습니다.')
    if expected['owner_profile_id']==target_profile_id:
        raise ValueError('현재 실행 계정과 대상이 같습니다.')
    directory,path,declared=_paths(home,expected['thread_id'])
    directory.mkdir(exist_ok=True)
    lock=directory/(expected['thread_id']+'.lock')
    try:lock.mkdir()
    except FileExistsError:
        raise RuntimeError('이 대화의 인계 잠금이 있습니다. 시간이 지났다는 이유로 잠금을 빼앗지 않습니다.') from None
    try:
        with _authority_guard(directory/(expected['thread_id']+'.guard')),_writer_guard(home,expected['thread_id']):
            return _replace_owner(home,directory,path,expected,target_profile_id)
    finally:
        lock.rmdir()


class AuthorityBatchIncomplete(RuntimeError):
    def __init__(self,changed,uncertain):
        super().__init__('실행 소유 기록 일부를 갱신하지 못했습니다. 인계 복구 상태를 확인해야 합니다.')
        self.changed=changed;self.uncertain=uncertain


def transfer_many(home,expected_grants,target_profile_id,*,release_verified):
    """Hold every tree guard until all owner records have been published.

    The caller journals intent before entering. A failure never rolls back epochs.
    """
    target_profile_id=str(UUID(target_profile_id))
    grants=[validate(dict(value)) for value in expected_grants]
    if release_verified is not True:
        raise RuntimeError('기존 실행 종료·기록 확정을 확인하기 전에는 인계하지 않습니다.')
    if not grants or len({g['thread_id'] for g in grants})!=len(grants):
        raise ValueError('인계할 대화 목록이 없거나 중복되었습니다.')
    if any(g['owner_profile_id']==target_profile_id for g in grants):
        raise ValueError('현재 실행 계정과 대상이 같습니다.')
    if len({(g['host_id'],g['store_id'],g['owner_profile_id']) for g in grants})!=1:
        raise ValueError('인계할 대화 트리의 저장소와 실행 소유자가 일치하지 않습니다.')
    paths={g['thread_id']:_paths(home,g['thread_id']) for g in grants}
    locks=[]
    try:
        for tid in sorted(paths):
            directory=paths[tid][0];directory.mkdir(exist_ok=True)
            lock=directory/(tid+'.lock')
            try:lock.mkdir()
            except FileExistsError:
                raise RuntimeError('이 대화의 인계 잠금이 있습니다. 잠금 소유자의 작업이 끝나야 합니다.') from None
            locks.append(lock)
        with ExitStack() as guards:
            for tid in sorted(paths):
                guards.enter_context(_authority_guard(paths[tid][0]/(tid+'.guard')))
            for tid in sorted(paths):
                guards.enter_context(_writer_guard(home,tid))
            for expected in grants:
                if read(home,expected['thread_id'])!=expected:
                    raise RuntimeError('대화의 실행 소유자가 변경되었습니다. 최신 상태를 다시 확인하세요.')
                if max(expected['epoch'],expected['revision'])>=MAX_COUNTER:
                    raise RuntimeError('실행 소유 버전 한도에 도달했습니다.')
            try:
                return [_replace_owner(home,*paths[g['thread_id']][:2],g,target_profile_id) for g in grants]
            except (OSError,ValueError,RuntimeError):
                changed=[];uncertain=False
                for expected in grants:
                    try:
                        actual=read(home,expected['thread_id'])
                        if actual!=expected:changed.append(actual)
                    except (OSError,ValueError,RuntimeError):uncertain=True
                raise AuthorityBatchIncomplete(changed,uncertain) from None
    finally:
        for lock in reversed(locks):lock.rmdir()


def _replace_owner(home,directory,path,expected,target_profile_id):
    temporary=None
    try:
        actual=read(home,expected['thread_id'])
        if actual!=expected:
            raise RuntimeError('대화의 실행 소유자가 변경되었습니다. 최신 상태를 다시 확인하세요.')
        if max(actual['epoch'],actual['revision'])>=MAX_COUNTER:
            raise RuntimeError('실행 소유 버전 한도에 도달했습니다.')
        updated={**actual,'owner_profile_id':target_profile_id,'epoch':actual['epoch']+1,'revision':actual['revision']+1}
        fd,temporary=tempfile.mkstemp(prefix=expected['thread_id']+'.',suffix='.tmp',dir=directory)
        with os.fdopen(fd,'w',encoding='utf-8') as file:
            json.dump(updated,file,ensure_ascii=False,sort_keys=True);file.flush();os.fsync(file.fileno())
        os.replace(temporary,path);temporary=None
        if os.name!='nt':
            fd=os.open(directory,os.O_RDONLY)
            try:os.fsync(fd)
            finally:os.close(fd)
        return updated
    finally:
        if temporary and os.path.exists(temporary):os.unlink(temporary)
