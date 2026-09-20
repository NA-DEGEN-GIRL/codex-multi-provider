# Input latency, revision 67

The reported delay becomes less noticeable after the workspace has been open
for a while. Revision 66's metadata-only trace supports investigating startup
contention separately from steady-state typing:

| Time after startup | Renderer probe median | Renderer probe p95 |
| --- | ---: | ---: |
| First 90 seconds | 171 ms | 749 ms |
| 90–240 seconds | 4 ms | 228 ms |
| After 240 seconds | 5 ms | 51 ms |

These are renderer IPC round trips, **not measured keystroke-to-paint latency**.
The trace also contains a 1,991 ms renderer probe and a 1,422 ms
`shell.selected_window` operation. Normal retained-window polling was around
110 ms. The native geometry scopes did not account for that whole delay;
synchronous WPF layout inside the selected-window path is an unnecessary
additional flush, but this does not establish it as the only cause of typing lag.

## Changes

- A state poll verifies the retained window lifetime without forcing another
  full WPF layout. Explicit profile selection, manual repair, native move/size
  events and the existing geometry watchdog retain their repair paths.
- The Electron main-process catalog refresh reads durable thread summaries.
  It previously hydrated eight complete turns as well, in every background
  profile, then asked the renderer to hydrate those turns again. The visible
  renderer still reads history and retains pagination, draft, local-turn and
  deletion protections. Missing summaries still cannot become native imports.
- A passive `beforeinput` guard also protects the first keystroke and
  deletion-to-empty, before a draft is present in the DOM. A 250 ms guard after
  composition completion protects IME commit. This defers sync hydration; it
  does not delay, forward or suppress input events.
- The existing two-second selected-renderer probe now also reads bounded
  numeric timing aggregates. Event Timing samples cover composer keyboard and
  composition events of at least 16 ms; long tasks are observed separately.
  At most 256 samples of each kind are retained for 30 seconds. A zero sample
  count does not prove zero latency, and unsupported observers are reported.
  Keys, text, DOM nodes, URLs and task identifiers are not retained or logged.

## Validation and limits

Node fixtures exercise catalog-summary-only reads, visible history refresh,
hidden profile coalescing, local-turn races, draft preservation, the first input
and IME commit guards, deletion/archival, bounded diagnostic samples and scalar
whitelisting. Existing Windows host self-tests cover geometry and independent
input behavior. No running profile is restarted or navigated for these checks.

Actual typing improvements require testing the new release. The pre-change
trace cannot demonstrate an after-change latency reduction. The new health
fields distinguish delayed event dispatch, event-to-paint duration and long
renderer tasks if the symptom remains.

Completed checks: 1,020 manager Python tests (one platform skip), 17 Node
fixtures, the Windows build and its native-host/control/notes/layout self-tests,
and the privacy scan. Both isolated desktop variants were prepared without
launching a user profile.

References: [WPF UpdateLayout](https://learn.microsoft.com/en-us/dotnet/api/system.windows.uielement.updatelayout)
and [W3C Event Timing](https://www.w3.org/TR/event-timing/).
