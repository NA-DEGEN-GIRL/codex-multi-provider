# Profile switch response diagnostics (38)

The fix37 log `shell-20260917-231202-93988.log` reports cached selection in the
same second as the click, but reports native keyboard focus several seconds
later. Focus timing includes the user's next click and does not prove a stall.
There was no dispatcher heartbeat, renderer liveness or operation duration.
An input-free, 22-second WM_NULL sample of the live manager/profile windows
recorded no timeouts (maximum 20 ms). It did not reproduce the reported switch.

## Changes

- `shell-*.performance.jsonl`: independent 250 ms background watchdog, UI
  heartbeat, slow synchronous stage durations, RPC/switch durations, input queue
  age, bounded 100 ms native response checks and 2-second resource snapshots.
  Memory/CPU/handles/threads include the selected renderer when available.
- Private desktop helper probes the visible renderer with a constant `0` IPC
  evaluation at most once every 2 seconds. An unresponsive renderer has at most
  one pending probe. It records latency/pending age/main-loop delay without
  reading DOM text, input, conversation content or credentials.
- Log copy includes the most recent 60 diagnostic records. Metadata files rotate
  at 4 MB, queues are bounded, disk writes run off the dispatcher. Diagnostic
  failures must not terminate the app.
- Repeating native layout corrections now run below input priority. Hidden
  profiles do not resize/clip their native windows until selected. Unchanged
  DWM thumbnail properties are not resubmitted.

These changes improve observability and remove concrete scheduling/redundant
work issues. They do not establish that every reported freeze was caused by
those issues or demonstrate a long-term memory leak.

## Verification

- `desktop_window_health_test.cjs`: no pre-load/hidden/wrong/closed window
  probes, one pending probe during a hang, actual latency and loop delay.
- Seven desktop archive/integrity tests pass.
- `control-center-responsiveness-test.json`: a separate windowless fixture
  deliberately blocks its dispatcher for 1.8 seconds. The background log
  records the stall and operation while blocked, and includes it in copied logs.
- All build-manager self-tests pass (native host, title controls, response
  monitor, notifications, notes).
- Real Electron render fixture `artifacts/results/desktop-render-5cbc5871`
  passes cold hidden start, resize, drift, fast/slow restore, profile visibility
  and manager-window capture; its real renderer health file is populated.
- Published manager: `artifacts/manager/releases/20260917-142644-400`.

No running user application was closed, navigated or focused for these tests.
The new diagnostics take effect on the next launch; an old running process
does not gain the new helper until closed and reopened.
