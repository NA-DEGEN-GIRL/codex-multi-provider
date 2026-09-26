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
        started, error = time.perf_counter(), None
        try:
            yield
        except BaseException as failure:
            # The exception class only: its message can carry paths or text.
            error = type(failure).__name__
            raise
        finally:
            self.record(profile_id, name, started, error is None, error=error)

    def record(self, profile_id, name, started, success=True, *, error=None, released_by=None):
        event = dict(at=datetime.now(timezone.utc).isoformat(), profile_id=profile_id,
                     phase=name, elapsed_ms=round((time.perf_counter()-started)*1000, 2),
                     success=success)
        if error:
            event['error'] = error
        if released_by:
            # A fixed code from profile_warmup (warmup_gate), never free text.
            event['released_by'] = released_by
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
