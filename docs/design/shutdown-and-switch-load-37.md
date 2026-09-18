# Managed shutdown and profile switch load (37)

## Evidence and scope

The shell log at 22:57:40 posted WM_CLOSE to managed roots 53280, 95992 and
58888. The explicit stop-all run 13 seconds later still found those roots and
their children. WM_CLOSE is a window operation, not proof of Electron app exit.
The 83-process total also included the original desktop and normal Chromium
children; it is not an 83-process leak measurement.

Cached profile switches already reuse the same HWND/PID. The sync adapters,
however, repeated renderer hydration three times for each delivery from the
main adapter (which already performs three durability reads). Hidden renderers
did the same work. Disposed manager objects were filtered but retained by strong
Sets. Successful thread/read traffic also rewrote the diagnostic file on the
shell dispatcher for each message. No long-duration heap growth has been proven.

## Changes

- Shell-owned, lifetime-marked windows receive an explicit shutdown lease.
  The private Electron adapter calls app.quit once, including before renderer
  readiness. The shell asynchronously waits up to 15 seconds for the verified
  process to exit, clears only its own lease, then closes. Timeout keeps the
  shell open and reports failure; no automatic forced termination is used.
- Explicitly detached windows and the original desktop are outside that scope.
  The service is asked to retire; existing multi-client/pending-work guards and
  idle retirement remain in effect. Empty fixture windows still close immediately.
- Renderer invalidations perform one successful merge; hidden renderers
  coalesce by task until visible. In-flight invalidations and bounded failure
  retries survive; drafts/local submissions remain protected. Disposed managers
  are removed from both adapter Sets.
- Successful thread/read and configRequirements/read are background log traffic;
  failures remain visible under the existing rate limiter.
- Stop-All-Codex.ps1 acquires a typed SafeHandle pointer and checks exit before
  termination, avoiding a null raw Handle during process exit races.

## Verification

- Node window-host suite: scoped one-shot app quit before renderer readiness;
  wrong HWND/dead owner rejected; prior geometry/presentation tests pass.
- Both sync adapter suites: hidden burst coalescing, single merge, concurrent
  invalidation, disposed owner release, drafts/submissions and retries pass.
- Real private Electron + production MainWindow close fixture:
  `artifacts/results/desktop-render-28400d0d/shutdown.json`, exit verified in
  1.013 seconds, no remaining processes under its private app directory.
- Real rendering fixture (hidden cold start, captures, resize, fast/slow
  minimize/restore, profile hide/show):
  `artifacts/results/desktop-render-00970c4b/wpf-host.json` passes.
- Real desktop with independent fixture writer:
  `artifacts/results/desktop-sync-cd1c7a11/report.json` passes all 12 checks,
  including visible updates without reopening and unsent draft preservation.
  No external model requests or user conversations used.
- build-manager.ps1 -SelfTest passes native host, title controls, notification
  activation, and 13 notes/checklist checks. Published manager release:
  `artifacts/manager/releases/20260917-140826-888`.
- Stop-All-Codex selection self-test: 8 checks; no user processes stopped.

Existing running binaries retain the old adapters. Apply this release after
finishing work and closing the previous managed processes once. Subsequent
normal close must report process exit, rather than merely a WM_CLOSE request.
