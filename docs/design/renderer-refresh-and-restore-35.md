# Visible history refresh and window lifecycle — revision 35

## Reproduced failure

Revision 34 refreshed the main-process AppServerManager, but the native renderer
has its own AppServerManager and transcript subscriptions. Main-store counters
reported success while the visible task stayed stale. The real desktop fixture
reproduced this: third message in the main store, only two messages on screen
(`artifacts/results/desktop-sync-d5075193`). Testing only the store was inadequate.

Both private bundles now verify and patch the main AND renderer entry points.
After a successful history read, the main process sends a native bridge event
containing task IDs. The renderer invalidates its own lookup caches and hydrates
the transcript through native subscriptions. It never reloads, navigates, focuses,
resumes a task just to refresh it, or replays a message as a new turn. Original-app
renderer notifications also publish ID-only invalidations back to the main
signal writer. Managed instances retain their existing proxy publisher.

Native local turn/start and turn/steer requests, streaming notifications and
resumes remain authoritative. A cancellation predicate checks again after I/O
so a user submission during a read cannot be overwritten. Native hydration can
recreate the composer when the latest turn changes. While any visible composer
has unsent text or an active IME composition, renderer history merges are deferred
and retained in the pending queue. They catch up after the draft is cleared or
submitted and the local response finishes. This explicitly favors preserving
input over refreshing a draft-containing viewer. No DOM text restoration is used.

## Window presentation

The revision 33 independent, unowned top-level input window is retained for
native inline Korean IME. The viewport never uses global TOPMOST. It follows
the manager's normal z-order; clicking the editor puts the manager directly
under it without transferring keyboard focus. Other foreground applications and
capture overlays stay above the group.

Minimize/restore now updates both Win32 visibility and Electron's compositor
visibility. A monotonically increasing presentation epoch preserves a fast hide/
show transition even when file polling misses the intermediate hidden lease.
One native hide/showInactive cycle is allowed per epoch, never repeated activation.
Geometry-independent visibility repair also covers restoring to unchanged bounds.

The custom title bar compensates for maximized nonclient margins using the actual
monitor work area, client origin and DPI, including negative monitor coordinates.

## Repeated network permission prompts

Immutable private executable paths accumulate separate Windows firewall decisions.
The observed native app listened on mDNS port 5353. The Windows bundles disable
Chromium MediaRouter and DialMediaRouteProvider discovery before app readiness,
merging existing disabled features. Voice/WebRTC, HTTPS and SSH are not disabled;
no firewall rule is added, removed or broadened. A disposable process with the
policy had no mDNS listener. Windows Security dialog behavior across user launches
still needs confirmation; this is not a claim to suppress every possible prompt.

## Validation

- Actual native desktop with disposable homes and loopback model responses:
  `artifacts/results/desktop-sync-9499e12c/report.json`. Visible second and third
  messages update without reopening; the third user message appears before the
  writer response completes. A native composer draft survives and pending history
  catches up after clearing it. Main and renderer report zero sync failures.
- Node regressions cover separate renderer delivery, local submit/stream races,
  draft deferral, metadata-only publication, missing-task isolation, restoration
  epochs and device-discovery policy.
- Bundle tests reject an unknown renderer entry point rather than publishing a
  main-only sync integration. Archive offsets/integrity and immutable inputs are
  checked for both bundle types.
- WPF/native tests exercise repeated minimize/restore, geometry repair, normal
  z-order, process identity, cleanup and title-bar DPI insets.

Offscreen HWND/compositor tests do not prove the final pixels of the user's
Settings screen or a particular screenshot tool's window capture mode. A top-level
viewport is a separate HWND; capture of the manager HWND alone is not equivalent
to desktop/region capture. Physical IME behavior remains the revision 33 design.

Loaded applications retain revision 34 code until closed normally once and reopened
through Open-Control-Center.cmd / Open-Synced-Codex.cmd. No live user process is
stopped, focused or navigated by this change or its fixtures.
