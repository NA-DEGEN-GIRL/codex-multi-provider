# Workspace notifications and viewport recovery — revision 40

## Symptoms and findings

The user was chatting in workspace profile 02, but clicking a Windows question
notification opened the default Codex application. The previous adapter only ran
after the native notification click callback; the failing click never reached
the shell's callback. Notification delivery now belongs to the workspace for
hosted local/SSH task notifications, with a dedicated activation URI.

Fix 39 logs also show native-visible, responsive windows with a fresh renderer
acknowledgement while the viewport was gray. Resizing restored content. In the
actual installed Owl runtime, `webContents.invalidate` is absent, so the old
optional repaint did nothing. A real hidden-desktop fixture additionally exposed
an initial lease replacement sharing violation that detached a healthy window.

## Notification behavior

- Hosted question, permission and completion notices are delivered over the
  existing CurrentUserOnly pipe. OS peer PID, HWND, current lease token and owner
  must agree. Native notification delivery is suppressed only after acceptance;
  unsupported routes and unavailable managers retain the native fallback.
- Windows toasts identify the profile, use escaped XML and a private
  `codex-workspace-<installation identity>://notification/<opaque ticket>` URI.
  The global `codex://` registration is unchanged. No toast action approves a
  request or submits a model prompt: it opens the task for the user to answer.
- Actual `turn/started` events record the last chatting profile per host+task.
  Simply viewing a shared task does not take over that task's notifications.
  Selected-task history is the fallback for tasks without turn metadata.
  Both survive manager restarts; removed profiles are ignored.
- A clicked ticket resolves the latest profile at click time. A running manager
  receives it over a private pipe; a stopped manager initializes, then opens it.
  Startup redirects preserve the ticket and single-instance forwarding precedes
  version redirects. Tickets contain no executable paths or command arguments.
- Native route dispatch is scoped to the exact hosted renderer and its own
  WindowManager. It waits for the task router, sends `navigate-to-route`, and
  acknowledges once. PID/HWND/token, expiry and duplicate IDs prevent an old
  command from moving another window. SSH host IDs remain part of the route.
- Avatar/popout routes no longer overwrite the hosted editor's selected-task
  record. This was observed in the real desktop integration test.

## Display behavior

- Failed viewport lease publication stays pending even during initial attach.
  A transient reader no longer causes detach; the existing bounded layout watch
  retries and logs recovery. Release/shutdown failures remain explicit.
- Only the selected hosted renderer has background throttling disabled. Hidden
  or detached windows regain the native app's requested setting.
- Selection/restore commits a one-pixel native bounds transition and immediately
  restores the exact viewport bounds. This works with Owl's supported API and
  gives its compositor a size transition, unlike an optional nonexistent
  invalidate call. It runs once per presentation epoch, not on a repaint timer.
  No focus transfer, parent/owner relationship, page reload or keyboard forwarding
  is introduced. IME remains owned by the native editor.
- The private desktop cache identity now includes patch implementation content,
  so integration-code changes cannot silently reuse an older patched archive.

## Validation and limits

Node tests cover scoped notification acceptance/fallback, cold router readiness,
separate auxiliary dispatchers, local/SSH paths, replay rejection and selected
task filtering. C# tests exercise a real Windows pipe, persisted destination
selection and an actual Windows toast with popup suppressed. Windows invokes its
registered URI handler in a windowless isolated process and returns the exact
ticket to the existing fixture listener. The fixture removes its toast and URI
registration afterward; it does not navigate or stop a user application.

The rendering fixture uses empty disposable homes and a loopback model fixture
for a seeded task. It exercises production hosting, real native route dispatch,
selected-task context, renderer frame scheduling, capture, geometry drift,
minimize/restore and lost-publication recovery. It is not a claim that every GPU,
monitor or long-running user workload has been reproduced.

Firewall inspection found persisted TCP/UDP allow rules for both fix 38 and fix
39 executable paths. All profiles in one build use the same executable. An
immutable desktop update changes the path and Windows can ask once for that new
binary; this is different from forgetting permission on each restart of the
same build. No firewall rules are added or relaxed by this change.

References: [Windows toast activation](https://learn.microsoft.com/sv-se/windows/apps/develop/notifications/app-notifications/toast-desktop-apps),
[ToastNotificationManagerCompat](https://learn.microsoft.com/en-us/dotnet/api/microsoft.toolkit.uwp.notifications.toastnotificationmanagercompat?view=win-comm-toolkit-dotnet-7.1),
[Chromium window occlusion](https://chromium.googlesource.com/chromium/src/+/c1ce99f4ed59/docs/windows_native_window_occlusion_tracking.md).
