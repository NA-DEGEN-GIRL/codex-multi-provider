"""Select a versioned runtime without replacing a running executable."""
import hashlib,json
from pathlib import Path

_HASHES={}


def _digest(path):
    stat=path.stat();key=(str(path),stat.st_size,stat.st_mtime_ns)
    digest=_HASHES.get(key)
    if digest is None:
        with path.open('rb') as file:digest=hashlib.file_digest(file,'sha256').hexdigest()
        after=path.stat()
        if (after.st_size,after.st_mtime_ns)!=(stat.st_size,stat.st_mtime_ns):
            raise RuntimeError('검증 중 런타임 파일이 변경되었습니다.')
        _HASHES[key]=digest
    return digest


def resolve(root):
    root=Path(root).resolve();pointer=root/'artifacts/manager-runtime/current.json'
    if not pointer.is_file():
        return dict(runtime=str(root/'artifacts/runtime/codex.exe'),capabilities={},version='0.153.4')
    return load_release(root, pointer)


def load_release(root, pointer):
    """Verify a release without making it the active runtime for any profile."""
    root=Path(root).resolve();pointer=Path(pointer).resolve(strict=True)
    info=json.loads(pointer.read_text(encoding='utf-8-sig'))
    binary=Path(info['runtime']).resolve(strict=True)
    if not binary.is_relative_to(root/'artifacts/manager-runtime/releases'):
        raise RuntimeError('런타임 배포 경로가 올바르지 않습니다.')
    if _digest(binary)!=info.get('sha256'):
        raise RuntimeError('런타임 파일이 검증된 배포본과 다릅니다.')
    for name, expected in info.get('files', {}).items():
        companion=(binary.parent/name).resolve(strict=True)
        if companion.parent!=binary.parent or companion.name!=name:
            raise RuntimeError('런타임 구성 파일의 경로가 올바르지 않습니다.')
        if _digest(companion)!=expected:
            raise RuntimeError('런타임 구성 파일이 검증된 배포본과 다릅니다.')
    return {**info,'runtime':str(binary)}
