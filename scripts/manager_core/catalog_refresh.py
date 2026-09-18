"""Bounded background refresh of the common native record index."""
from copy import deepcopy
from datetime import datetime,timezone
import threading
import time
from .record_catalog import build


class CatalogRefresh:
    def __init__(self,root,*,interval=5,builder=build):
        self.root=root;self.interval=interval;self.builder=builder
        self.lock=threading.RLock();self.last_started=0;self.running=False;self.result=None
        self.error=None;self.completed_at=None
        self._stop=threading.Event();self._monitor=None

    def start(self,source_config):
        """Refresh while the backend lives, even when no UI requests state."""
        with self.lock:
            if self._monitor is not None and self._monitor.is_alive():return False
            self._stop.clear()
            def monitor():
                while not self._stop.is_set():
                    try:
                        config=source_config()
                        if config is not None:
                            sources,include_paginated=config
                            self.ensure(sources,include_paginated=include_paginated)
                    except (OSError,ValueError,RuntimeError):
                        with self.lock:self.error='기록 색인을 갱신하지 못했습니다. 이전 색인을 유지합니다.'
                    if self._stop.wait(max(self.interval,.01)):break
            self._monitor=threading.Thread(target=monitor,name='record-catalog-monitor',daemon=True)
            self._monitor.start()
        return True

    def stop(self):
        self._stop.set()
        with self.lock:monitor=self._monitor
        if monitor is not None:monitor.join(timeout=2)

    def _run(self,sources,include_paginated):
        try:
            result=self.builder(self.root,sources,include_paginated=include_paginated)
            with self.lock:
                self.result=result;self.error=None
                self.completed_at=datetime.now(timezone.utc).isoformat()
            return result
        except (OSError,ValueError,RuntimeError):
            with self.lock:self.error='기록 색인을 갱신하지 못했습니다. 이전 색인을 유지합니다.'
            raise
        finally:
            with self.lock:self.running=False

    def ensure(self,sources,*,include_paginated):
        with self.lock:
            if self.running:
                if self.result:return deepcopy(self.result)
                raise RuntimeError('전체 기록 색인을 만드는 중입니다. 잠시 후 다시 열어주세요.')
            self.running=True;self.last_started=time.monotonic()
        return self._run(deepcopy(sources),include_paginated)

    def refresh(self,sources,*,include_paginated):
        with self.lock:
            if self.running or time.monotonic()-self.last_started<self.interval:return False
            self.running=True;self.last_started=time.monotonic()
        def refresh_index():
            try:self._run(deepcopy(sources),include_paginated)
            except (OSError,ValueError,RuntimeError):pass
        threading.Thread(target=refresh_index,name='record-catalog-refresh',daemon=True).start()
        return True

    def status(self):
        with self.lock:
            return dict(refreshing=self.running,observed_at=self.completed_at,error=self.error,
                        refresh_interval_seconds=self.interval,
                        entries=self.result.get('entries') if self.result else None,
                        complete=self.result.get('complete',False) if self.result else False)
