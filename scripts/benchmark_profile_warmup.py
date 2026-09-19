"""Synthetic warmup scheduling benchmark. No desktop, network or real accounts."""
import argparse
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace

from manager_core.profile_warmup import ProfileWarmup
from manager_core.store import Store


def run(workers, profiles, prepare_ms, window_ms):
    with tempfile.TemporaryDirectory(prefix='warmup-benchmark-') as temporary:
        store = Store(Path(temporary))
        for index in range(profiles):
            store.add_profile(f'fixture-{index}')
        admission = threading.Lock()
        def launch(_):
            with admission:
                time.sleep(prepare_ms/1000)
            time.sleep(window_ms/1000)
            return dict(state='launched', profile=dict(status='running', window_handle=1))
        jobs = []
        def spawn(fn):
            worker = threading.Thread(target=fn)
            jobs.append(worker)
            worker.start()
        warmup = ProfileWarmup(store, SimpleNamespace(observe=lambda _: dict(status='not_started')),
                               launch, spawn=spawn, health=lambda _: {}, max_workers=workers)
        started = time.perf_counter()
        warmup.start()
        jobs[0].join()
        return dict(workers=workers, elapsed_ms=round((time.perf_counter()-started)*1000),
                    ready=warmup.status()['counts']['ready'])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profiles', type=int, default=8)
    parser.add_argument('--prepare-ms', type=int, default=100)
    parser.add_argument('--window-ms', type=int, default=300)
    args = parser.parse_args()
    if not 1 <= args.profiles <= 32 or not 0 <= args.prepare_ms <= 1000 or not 0 <= args.window_ms <= 1000:
        parser.error('Use 1..32 profiles and 0..1000 ms per phase.')
    print(json.dumps(dict(synthetic=True, profiles=args.profiles,
                         serialized_prepare_ms=args.prepare_ms, window_wait_ms=args.window_ms,
                         results=[run(n, args.profiles, args.prepare_ms, args.window_ms) for n in (1, 2, 4, 8)])))
