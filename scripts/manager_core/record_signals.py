"""Bounded, content-free record invalidations; never wait for disk on RPC pumps."""
from collections import OrderedDict
import json
import os
from pathlib import Path
import threading
import time
from uuid import UUID, uuid4


METHODS = frozenset({'thread/started', 'thread/name/updated', 'thread/settings/updated',
    'thread/project/updated',
    'thread/archived', 'thread/unarchived', 'thread/deleted', 'turn/started',
    'turn/completed', 'item/started', 'item/completed', 'item/agentMessage/delta'})


class RecordSignals:
    def __init__(self, directory, profile_id):
        self.path = Path(directory) / (str(UUID(profile_id)) + '.json')
        self.generation = uuid4().hex
        self.changed = OrderedDict()
        self.visibility = OrderedDict()
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.dirty = threading.Event()
        self.sequence = 0
        self.worker = threading.Thread(target=self._run, daemon=True)
        self.worker.start()

    def observe(self, message):
        if 'id' in message or message.get('method') not in METHODS:
            return
        params = message.get('params') or {}
        if not isinstance(params, dict):
            return
        thread = params.get('thread')
        identity = thread.get('id') if isinstance(thread, dict) else params.get('threadId')
        try:
            identity = str(UUID(identity))
        except (ValueError, TypeError, AttributeError):
            return
        with self.lock:
            kind = {'thread/deleted': 'deleted', 'thread/archived': 'archived',
                    'thread/unarchived': 'unarchived'}.get(message['method'])
            if kind:
                self.visibility[identity] = kind
                self.visibility.move_to_end(identity)
                while len(self.visibility) > 4096:
                    self.visibility.popitem(last=False)
            kind = self.visibility.get(identity, 'changed')
            self.sequence += 1
            self.changed.pop(identity, None)
            self.changed[identity] = [identity, self.sequence, 'local', kind]
            while len(self.changed) > 256:
                self.changed.popitem(last=False)
        self.dirty.set()

    def _flush(self):
        if not self.dirty.is_set():
            return
        with self.lock:
            self.dirty.clear()
            value = dict(version=2, generation=self.generation, updated=time.time(),
                         changes=list(self.changed.values()))
        temporary = self.path.with_suffix('.tmp-' + self.generation)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(value, separators=(',', ':')), encoding='utf-8')
            os.replace(temporary, self.path)
        except OSError:
            self.dirty.set()
        finally:
            try: temporary.unlink(missing_ok=True)
            except OSError: pass

    def _run(self):
        while not self.stop.wait(.4):
            self._flush()
        self._flush()

    def close(self):
        self.stop.set()
        self.worker.join(timeout=2)
