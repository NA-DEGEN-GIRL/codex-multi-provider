"""Identify explicit read-only projections for passive activity accounting."""
import json
from pathlib import Path
from uuid import UUID
from .catalog_origin import CatalogOrigins


class CatalogProjectionIds:
    def __init__(self, path):
        self.path = Path(path) if path else None
        self.stamp = None
        self.identities = frozenset()
        self.origins = CatalogOrigins()

    def observe_thread(self, thread):
        self.origins.observe_thread(thread)

    def forget(self, identity):
        self.origins.forget(identity)

    def close(self):
        self.origins.close()

    def __call__(self, identity):
        if self.origins(identity):
            return True
        if self.path is None:
            return False
        try:
            stat = self.path.stat()
            if stat.st_size > 4 * 1024 * 1024:
                return False
            stamp = (stat.st_mtime_ns, stat.st_size)
            if stamp != self.stamp:
                data = json.loads(self.path.read_text(encoding='utf-8'))
                if data.get('version') == 3 and data.get('hostId') == 'local':
                    # V3 enumerates homes. Passive IDs come only from native
                    # metadata observed above, not an inferred UUID pattern.
                    self.identities, self.stamp = frozenset(), stamp
                    return False
                entries = data['entries']
                if data['version'] != 1 or data['hostId'] != 'local' or len(entries) > 4096:
                    return False
                projections = {str(UUID(entry['projectionThreadId'])) for entry in entries}
                canonical = {str(UUID(entry['threadId'])) for entry in entries}
                if len(projections) != len(entries) or projections.intersection(canonical):
                    return False
                self.identities, self.stamp = frozenset(projections), stamp
            return identity in self.identities
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return False
