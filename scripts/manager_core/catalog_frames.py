"""Complete metadata snapshots as bounded JSON lines, for SSH and private cache."""
import json
import os
from pathlib import Path
import tempfile
import time

MAX_FRAME_BYTES = 8 * 1024 * 1024
PAGE_ROWS = 256


def _line(value):
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8') + b'\n'
    if len(encoded) > MAX_FRAME_BYTES:
        raise ValueError('Catalog metadata frame exceeds its size limit')
    return encoded


def write(stream, value):
    stream.write(_line(dict(kind='catalog', version=1, metadata={k: v for k, v in value.items() if k != 'conversations'})))
    rows, count = value['conversations'], 0
    for start in range(0, len(rows), PAGE_ROWS):
        page = rows[start:start + PAGE_ROWS]
        stream.write(_line(dict(kind='rows', data=page)))
        count += len(page)
    stream.write(_line(dict(kind='end', count=count)))


def read(stream):
    def frame():
        line = stream.readline(MAX_FRAME_BYTES + 1)
        if not line or len(line) > MAX_FRAME_BYTES or not line.endswith(b'\n'):
            raise ValueError('Incomplete or oversized catalog metadata frame')
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError('Invalid catalog metadata frame')
        return value
    header = frame()
    if header.get('kind') != 'catalog' or header.get('version') != 1 or not isinstance(header.get('metadata'), dict):
        raise ValueError('Invalid catalog metadata header')
    result = dict(header['metadata'], conversations=[])
    while True:
        value = frame()
        if value.get('kind') == 'end':
            if type(value.get('count')) is not int or value['count'] != len(result['conversations']) or stream.read(1):
                raise ValueError('Incomplete catalog metadata snapshot')
            return result
        if value.get('kind') != 'rows' or not isinstance(value.get('data'), list) or not 1 <= len(value['data']) <= PAGE_ROWS:
            raise ValueError('Invalid catalog metadata page')
        result['conversations'].extend(value['data'])


def atomic_write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            write(stream, value)
            stream.flush()
            os.fsync(stream.fileno())
        deadline = time.monotonic() + 1
        while True:
            try:
                os.replace(temporary, path)
                break
            except PermissionError as error:
                if os.name != 'nt' or getattr(error, 'winerror', None) not in (5, 32, 33) or time.monotonic() >= deadline:
                    raise
                time.sleep(.01)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
