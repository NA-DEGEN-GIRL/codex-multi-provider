# Electron background work, revision 65

Revision 64 overlapped profile preparation and cached the window lease. This pass
reduces repetitive work in the injected Electron synchronization adapters. It
does not change Chromium flags, GPU acceleration, native window ownership, IME
handling, or account/process isolation.

## Changes

- Local and SSH project adapters previously enumerated, read, parsed and merged
  every writer file on each 700 ms tick. They now share a change-file reader that
  parses changed files only. Record invalidations and project-membership proofs
  use the same reader instead of enumerating and checking every peer each tick.
- Directory watches are hints. A two-second metadata sweep recovers missed
  events; systems without working watches retain polling. Normal watch events
  are consumed on the existing adapter cadence (400 or 700 ms). Atomic replacement,
  directory recreation and writes arriving during an asynchronous read are
  handled. Corrupt files are isolated and retried with a bound. A directory scan
  failure still prevents project initialization from seeding stale declarations.
- Renderer-to-main metadata notifications for streamed text are coalesced over
  200 ms. The native text stream is untouched. Completion, project changes,
  deletion and archive events remain immediate; stale queued deltas cannot
  resurrect a deleted/archived task.
- Renderer transcript refresh uses a pending-work timer instead of an always-on
  400 ms timer. Hidden renderers retain invalidations and refresh on visibility.
  Existing local-turn, draft, composition and in-flight request guards remain.
- Native manager disposal releases adapter references immediately, with a
  30-second cleanup fallback while managers remain. File watchers are closed on
  app shutdown. Pending notification maps and file fingerprints are bounded.
- A partial text notification without a turn ID now preserves the known active
  turn ID. Otherwise an explicit completion could leave refresh waiting for an
  already completed turn. Both main and renderer regression fixtures cover this.

Both managed profiles and the synchronized original-app bundle load the new
helper before its consumers. Its content hash participates in bundle identity,
so an older prepared copy cannot silently omit it.

## Reproducible operation counts

These are controlled Node fixtures, **not measured CPU, memory, or startup-time
improvements in a live Codex session**.

| Workload | Previous behavior | Revision 65 |
| --- | ---: | ---: |
| Eight unchanged project files, 61 polls across six seconds: file reads/parses | 488 | 8 |
| Same workload: directory enumeration | 61 | 4 |
| One turn start and 1,000 text deltas within 200 ms: renderer metadata IPC messages | 1,001 | 2 |
| Renderer refresh while idle or hidden | Repeated 400 ms tick | No refresh timer |

The IPC burst is deliberately synthetic. Real traffic savings depend on delta
frequency; item lifecycle and other non-delta events are still sent immediately.
A missed file-watch event can take about two seconds plus the adapter cadence to
be noticed. This is recovery behavior, not a guarantee of instantaneous
cross-profile transcript rendering.

## Validation and limits

The fixtures exercise unchanged/changed files, watch loss, atomic replacement,
concurrent writes, corruption, UTF-8 byte limits, disposal, hidden-window catch-up,
retry scheduling, preservation of drafts and local streams, local/SSH project
add/move/delete, archive/unarchive, and cross-provider resume. Bundle tests verify
helper injection order for both app variants. Native shell self-tests are run by
`scripts/build-manager.ps1 -SelfTest` before publishing a release pointer.

Validation completed for this revision:

- 1,011 manager Python tests: successful, with one platform-dependent skip.
- 16 Node desktop fixture scripts, including the actual packaged notification
  callback: successful.
- Release build and native shell self-tests: successful.
- Managed and synchronized original copies prepared against desktop
  `26.915.4065.0`; six changed runtime adapters match the published bundle bytes.
- Tracked/unignored source inventory and Gitleaks scan: no private username,
  local checkout path, personal project/host names, or detected secrets.

Live session CPU/memory profiling and interactive startup measurements remain
separate validation. The build does not restart or navigate a live user session.
Additional candidates include duplicate background SSH connections and the
cost of one isolated Electron runtime per profile; neither is changed here.

The approach follows Electron's guidance to measure bottlenecks, avoid blocking
the main process, and reduce unnecessary startup work. See the official
[Electron performance guide](https://www.electronjs.org/docs/latest/tutorial/performance)
and [Node asynchronous filesystem API](https://nodejs.org/api/fs.html#promises-api).
