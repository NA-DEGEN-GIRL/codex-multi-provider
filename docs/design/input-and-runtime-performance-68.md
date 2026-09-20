# Input and runtime performance, revision 68

## What revision 67's live trace establishes

The retained-window path no longer spends roughly 100–125 ms on each periodic
state refresh. Main-process record synchronization also reports zero history
refreshes: summaries replaced those duplicate transcript reads as intended.

Typing is not yet consistently responsive during startup. In the first 90
seconds the new passive composer timing captured input dispatch delays up to
495 ms and event-to-paint durations up to 768 ms. These are slow-event samples,
bounded to 256 entries per 30-second window, not every keystroke. The renderer
had long tasks up to 1,307 ms. After three minutes the renderer liveness probe
settled to a median of 5 ms; no typing conclusion can be drawn from a quiet
window with no input samples. Startup content and activity differ between runs,
so this is not a controlled before/after benchmark.

## Windows resource sampling

The old diagnostic loop created a fresh `Process` object for every PID every two
seconds and requested memory, handles, thread count and CPU. Obtaining process
information through this path performs a systemwide process snapshot and then
filters it by PID. Consequently the diagnostics themselves consumed CPU and
temporary memory repeatedly as more profiles were attached.

An isolated .NET 10.0.8 benchmark sampled only its own process, ten times:

| Collector | Total wall time | CPU time | Managed allocation |
| --- | ---: | ---: | ---: |
| Previous `Process` property sequence | 275.96 ms | 281.25 ms | 39,600 bytes |
| Product native collector, including exit check | 0.073 ms | Below accounting resolution | 320 bytes |

The native path reads memory, CPU and handle counts directly. Thread counts are
intentionally omitted rather than reintroducing a systemwide enumeration.
These measurements demonstrate collector cost, **not a measured typing speedup**.
Counters are sequential samples, not an atomic system snapshot. The implementation
does not cache PID handles and treats vanished/inaccessible processes as missing
samples; normal UI heartbeat and renderer-health sampling remain in place.
An independent allocation fixture touched 16 MiB and both collectors measured
identical private-memory growth (16,814,080 bytes) and working-set growth
(16,781,312 bytes). CPU counters were checked against current-process CPU time;
owned event handles and invalid/exited processes were also tested.

Implementation references: [.NET 10.0.8 Windows process snapshots](https://raw.githubusercontent.com/dotnet/runtime/v10.0.8/src/libraries/System.Diagnostics.Process/src/System/Diagnostics/ProcessManager.Win32.cs),
[GetProcessMemoryInfo](https://learn.microsoft.com/en-us/windows/win32/api/psapi/nf-psapi-getprocessmemoryinfo),
[GetProcessTimes](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-getprocesstimes).

## Electron work scheduling

Preloaded profiles still receive and retain all invalidations. Hidden main
windows coalesce ordinary catalog refreshes to two-second batches. Selecting a
profile bypasses that gate on the next existing poll (400 ms), plus the ordinary
read time. Archive, restore and deletion signals bypass batching even while
hidden. Publication, local-turn guards, durable-read retries and host separation
are preserved.

The renderer now distinguishes a visible app window from the particular task
currently open in that window. Inactive tasks receive summaries without
rebuilding their cached transcript. The native active-conversation API queues
history catch-up when an existing task is activated, outside the native React
retention effect. Drafts, IME composition and local turns still defer hydration.
Dirty state is bounded and conservative on eviction; unknown or read-only
adapters retain the previous full-history behavior. Disconnected hosts retain
bounded pending updates without an idle timer or starving connected hosts.

## Periodic quota verification

Every account quota probe previously rehashed both the installed and staged CLI,
even though neither executable had changed. Those digests now use the existing
bounded publication cache. Reuse checks the opened file's identity, size, mtime
and content-change timestamp; missing stamps disable reuse. First staging and
actual changes still require full SHA-256 verification.

The cache also refuses timestamps less than one second old or in the future.
This covers ordinary same-clock-tick rewrites with a restored mtime without
adding a sleep to the service. Tests use controlled clocks and real digest
calls: settled files require no additional hashing, while changes and missing
stamps still require verification. This is a local performance cache, not a
security boundary against adversarial control of metadata or the system clock.

## Architecture conclusions

The app currently combines WPF/Win32 presentation, a Rust supervisor, Python
orchestration and separate native Electron instances for isolated profiles.
Changing the shell language alone does not remove Electron transcript work or
the memory cost of those instances. The higher-value sequence is:

1. Remove redundant OS enumeration and duplicate transcript hydration.
2. Bound background initialization/synchronization without delaying the selected
   task or losing updates.
3. Measure remaining costs, then replace specific Python hot paths with native
   Windows or Rust code where their measured cost justifies it.

Further review candidates include keyed WPF row updates and cached presentation
values, coalesced selected-task file reads, asynchronous presentation-ack reads,
avoiding hidden-host mirror geometry probes, and batching service identity
queries. Desktop and runtime identity queries refer to different PIDs, so they
cannot simply be deduplicated. Profile initialization also repeats shared
configuration reconciliation; any caching there must preserve additions and
deletions across accounts. They are separate follow-ups;
window ownership/input-queue changes would risk the existing IME and focus fixes.

Revision 68 requires a new process launch to take effect. The current conversation
was not restarted or redirected. End-to-end typing latency after revision 68 is
not established by these isolated tests; it needs a fresh live trace.

## Verification

- Manager Python suite: 1,031 tests, one intentional skip.
- Desktop JavaScript: 19 fixtures, including a method extracted from the newly
  packaged desktop. Activation during an unrelated failed task's retry now
  advances the timer without shortening that failed task's backoff.
- Release build and Windows self-tests passed: native viewport, window controls,
  responsiveness/counters, notifications, notes, layout, ordering and skills.
- Source/privacy scan found no secret leaks; runtime data and live logs stay
  outside version control.
