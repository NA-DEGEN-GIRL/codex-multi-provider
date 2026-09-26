"""Match the installed SSH implementation, independently of its package version."""
from functools import lru_cache
import hashlib
from pathlib import Path

# Native commands captured and exercised by tests/fixtures/native_ssh_*.json.
# A fixture alone does not verify a source: 26.917 has one for its decoded
# commands but still runs validate-each-native-command until it is listed here.
VERIFIED_SOURCES = frozenset({
    'a509a763f6e70e5076c7907ef3871418b47dc6adcd6b47692ffac5c8e0e1c266',
    '471f06dfcda15de10196f701504244c6f412d7ed401c155efc56a427a89a3195',
    'b55be874a9b5a262c09a7945df38cec9b0ce8f14bd584ef73d6feca301ed90b4',
})


def fingerprint(executable):
    archive = Path(executable).resolve().parent / 'resources/app.asar'
    try:
        info = archive.stat()
        return _fingerprint(archive, info.st_size, info.st_mtime_ns)
    except (OSError, ValueError, KeyError, TypeError):
        # Local profile launch does not depend on native SSH support.
        return 'unavailable'


@lru_cache(maxsize=8)
def _fingerprint(archive, size, modified):
    from .desktop_bundle import read_header, _entries
    matches = []
    with archive.open('rb') as source:
        header, base = read_header(source)
        for name, entry in _entries(header):
            if (name.startswith('.vite/build/') and name.endswith('.js')
                    and entry['size'] <= 32 * 1024 * 1024):
                source.seek(base + int(entry['offset']))
                body = source.read(entry['size'])
                if b'CODEX_REMOTE_PAYLOAD' in body:
                    matches.append(hashlib.sha256(body).hexdigest())
    return matches[0] if len(matches) == 1 else 'unavailable'
