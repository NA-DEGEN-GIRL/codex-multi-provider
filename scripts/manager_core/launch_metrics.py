"""Bounded local launch timings; never record credentials, paths or error text."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import threading
import time


class LaunchMetrics:
    _lock = threading.Lock()

    def __init__(self, directory):
        self.path = directory / 'logs' / 'profile-launch.performance.jsonl'

    @contextmanager
    def phase(self, profile_id, name):
        started, success = time.perf_counter(), False
        try:
            yield
            success = True
        finally:
            self.record(profile_id, name, started, success)

    def record(self, profile_id, name, started, success=True):
        event = dict(at=datetime.now(timezone.utc).isoformat(), profile_id=profile_id,
                     phase=name, elapsed_ms=round((time.perf_counter()-started)*1000, 2),
                     success=success)
        try:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if self.path.exists() and self.path.stat().st_size >= 2*1024*1024:
                    self.path.replace(self.path.with_suffix('.jsonl.1'))
                with self.path.open('a', encoding='utf-8') as output:
                    output.write(json.dumps(event, separators=(',', ':'))+'\n')
        except OSError:
            # Diagnostics must never prevent a profile from opening.
            pass
