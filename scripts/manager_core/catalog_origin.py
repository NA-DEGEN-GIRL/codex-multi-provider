"""Passive identities explicitly identified by the authenticated native server.

This consumes existing thread responses/notifications, never initiates an SSH
catalog query and never treats a user prompt or tool-result body as metadata.
"""
import sqlite3
import threading
from uuid import UUID


def _uuid(value):
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError('Invalid managed record identity.')
    return value


def validated_origin(thread):
    extra = thread.get('extra')
    origin = extra.get('managedRecord') if isinstance(extra, dict) else None
    if origin is None:
        return None
    identity = _uuid(thread.get('id'))
    if (not isinstance(origin, dict)
            or set(origin) != {'hostId', 'sourceStoreId', 'canonicalThreadId'}
            or origin['hostId'] != 'local'
            or not isinstance(origin['sourceStoreId'], str)
            or not 1 <= len(origin['sourceStoreId']) <= 256
            or any(ord(char) < 32 for char in origin['sourceStoreId'])
            or _uuid(origin['canonicalThreadId']) == identity
            or thread.get('canAcceptDirectInput') is not False
            or 'path' not in thread or thread['path'] is not None):
        raise ValueError('Invalid read-only managed record origin.')
    return dict(origin)


class CatalogOrigins:
    def __init__(self):
        self._lock = threading.RLock()
        self._db = None
        self._closed = False
        self._failed = False

    def _connect(self):
        if self._closed:
            raise ValueError('Managed record observer is closed.')
        if self._db is None:
            # SQLite owns and deletes this session-only temporary database. Store
            # only verified record IDs and store IDs, never history, credentials
            # or source paths. The origin also identifies explicit title edits.
            # A bounded page cache avoids both unbounded RAM and LRU eviction of
            # still-open records, which would misclassify their live events.
            self._db = sqlite3.connect('', check_same_thread=False, isolation_level=None)
            self._db.executescript('PRAGMA page_size=4096; PRAGMA cache_size=-512; '
                'PRAGMA max_page_count=16384; PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; '
                'CREATE TABLE origins (id BLOB PRIMARY KEY, source TEXT NOT NULL, canonical TEXT NOT NULL) WITHOUT ROWID;')
        return self._db

    def __call__(self, thread_id):
        try:
            identity = UUID(_uuid(thread_id)).bytes
        except (ValueError, TypeError, AttributeError):
            return False
        with self._lock:
            if self._failed or self._db is None:
                return False
            try:
                return self._db.execute('SELECT 1 FROM origins WHERE id=?', (identity,)).fetchone() is not None
            except sqlite3.Error:
                self._failed = True
                return False  # Storage failure must never hide actual work.

    def forget(self, thread_id):
        try:
            identity = UUID(_uuid(thread_id)).bytes
        except (ValueError, TypeError, AttributeError):
            return
        with self._lock:
            if self._failed:
                raise ValueError('Managed record membership storage unavailable.')
            try:
                if self._db is not None:
                    self._db.execute('DELETE FROM origins WHERE id=?', (identity,))
            except sqlite3.Error:
                self._failed = True
                raise ValueError('Managed record membership storage unavailable.') from None

    def lookup(self, thread_id):
        identity = UUID(_uuid(thread_id)).bytes
        with self._lock:
            if self._failed or self._closed:
                raise ValueError('Managed record membership storage unavailable.')
            if self._db is None:
                return None
            row = self._db.execute('SELECT source, canonical FROM origins WHERE id=?', (identity,)).fetchone()
            return dict(hostId='local', sourceStoreId=row[0], canonicalThreadId=row[1]) if row else None

    def observe_thread(self, thread):
        with self._lock:
            self.forget(thread.get('id'))
            origin = validated_origin(thread)
            if origin is not None:
                try:
                    self._connect().execute('INSERT INTO origins VALUES (?,?,?)',
                        (UUID(thread['id']).bytes, origin['sourceStoreId'], origin['canonicalThreadId']))
                except sqlite3.Error:
                    self._failed = True
                    raise ValueError('Managed record membership storage unavailable.') from None

    def close(self):
        with self._lock:
            self._closed = True
            if self._db is not None:
                self._db.close()
                self._db = None
