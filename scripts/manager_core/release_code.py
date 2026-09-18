"""Select frozen release code independently from mutable workspace/user data."""
import json
from pathlib import Path
import re


def script_path(root, relative):
    bundled = Path(__file__).resolve().parents[2]
    base = bundled if (bundled / 'runtime-manifest.json').is_file() else Path(root)
    return base / relative


def runtime_revision(proxy):
    if not proxy:
        return None
    manifest = Path(proxy).parent / 'runtime-manifest.json'
    if not manifest.exists():
        return None  # Pre-bundle release: require the one-time legacy transition.
    value = json.loads(manifest.read_text(encoding='utf-8'))
    revision = value.get('runtime_revision')
    if value.get('version') != 1 or not isinstance(revision, str) or not re.fullmatch('[0-9a-f]{64}', revision):
        raise RuntimeError('관리 실행 코드의 배포 정보를 확인하지 못했습니다.')
    return revision
