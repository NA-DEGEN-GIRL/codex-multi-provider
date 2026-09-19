# Profile startup performance — revision 64

The workspace shell is WPF/C#, the supervisor and process broker are Rust, and
profile/configuration orchestration is Python. Each account's desktop remains
an isolated Electron application. This change overlaps independent application
initialization; it does not replace Electron or merge account processes.

## Changes

- Warmup runs at most four launch callbacks concurrently. A selected queued
  profile takes the next available slot. Existing windows are not relaunched or
  focused. Shutdown cancels queued work and waits for admitted callbacks to finish.
- Configuration preparation, process creation and identity publication retain
  the global launch/update fence. The up-to-eight-second first-window wait moves
  outside that fence, including the local-first SSH path. SSH gate/manifest
  publication completes before release; remote reconciliation remains separate.
- Same-service launch preparations queue before the cross-process lock timeout.
  A cold bundle preparation cannot consume other warmup workers' admission
  timeout. The cross-process timeout and update ownership checks remain intact.
- A late window waiter checks generation, process birth and removal before
  publishing an HWND. It cannot overwrite a replacement profile lifetime.
- Launches reuse the same personal-skill and plugin reconcilers as background
  synchronization. Their existing change signatures decide whether to reconcile;
  a launch no longer constructs a new reconciler and forces a complete pass.
- Electron window guards cache parsed host leases by file metadata, invalidated
  by the lease's file watcher. The fallback timer still detects missed changes.
  Owner liveness, HWND identity, deletion and size checks remain active. This
  avoids repeated synchronous reads/parsing; metadata checks still run. Native
  geometry, IME, compositor settings and focus behavior are unchanged.
- The installed desktop package `26.915.4065.0` changed two bundled identifiers:
  the route effect's React binding and the reasoning-effort validator. Exact
  compatibility variants were added after inspecting both call sites. Archive
  ambiguity checks, integrity checks and native effort validation remain intact.

This follows Electron's recommendation to measure actual bottlenecks and reduce
blocking main-process work: [Electron performance guide](https://www.electronjs.org/docs/latest/tutorial/performance).

## Evidence

`python scripts/benchmark_profile_warmup.py` uses temporary stores, eight fake
profiles, 100 ms of serialized preparation and 300 ms of independent window
waiting. It never opens an application, contacts SSH, or uses account credentials.

| Concurrent callbacks | Total simulated time |
| --- | ---: |
| 1 | 3,219 ms |
| 2 | 1,715 ms |
| 4 | 1,113 ms |
| 8 | 1,112 ms |

These results demonstrate scheduling behavior, **not measured Codex startup
improvement**. Four slots avoid extra simultaneous initialization without losing
throughput in this fixture. Actual disk/cache/network conditions can differ.

Event-controlled tests cover bounded concurrency, queued selection priority,
shutdown, same-service queuing, independent window waits, update/SSH fencing,
replacement generations and PID reuse. The Electron VM fixture verifies repeated
guard calls do not reread an unchanged lease, with watch, polling, deletion and
dead-owner cases. The manager Python suite ran 1,008 cases with one skip; subsequent
desktop compatibility checks passed 16 cases. Release self-tests cover native
hosting and the existing UI behavior. Legacy lab launcher tests are outside this release gate; an
existing Flash test still assumes the upstream-only lab generates an external
agent role, contrary to its current configuration generator.

## Next real startup

`work/control-center/logs/profile-launch.performance.jsonl` records profile ID,
phase, elapsed milliseconds and success. It rotates at 2 MiB, retaining one older
file. It records no paths, aliases, credentials, model prompts or exception text.

Phases include admission wait, profile configuration, shared skills/plugins,
desktop bundle preparation, environment construction, prepare-and-spawn and
first-window wait. Parent phases include their children and must not be summed.
`show_total` excludes the deferred window wait on the local-first SSH path;
`ssh_launch_admission_wait` measures its outer admission wait separately.

Compare the first selected profile and the last ready background profile after
a normal restart, alongside the shell's existing responsiveness/resource log.
Separate first preparation after an update from later launches with a prepared
bundle. Do not restart an active user task just to obtain a benchmark.

Further Electron memory reduction needs measurements of each process tree.
Hidden renderers already defer transcript hydration and restore native background
throttling; unloading them would trade immediate switching for reload latency.
Rust conversion or CUDA is not a substitute for removing serialized waits.
