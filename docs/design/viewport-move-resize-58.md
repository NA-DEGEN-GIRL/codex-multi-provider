# Viewport movement and explicit repair

The editor remains an independent, unowned top-level window so Chromium keeps
its own activation and inline Korean IME. Cross-process parenting, ownership,
input forwarding and global topmost placement are not introduced.

Previously both the shell and the desktop lease reader could place the same
window. A delayed desktop bounds update could undo a newer shell placement.
Clipping also trusted cached dimensions, so removing the native window region
at unchanged size could leave a frame exposed indefinitely. Explicit restore
used the ordinary layout retry budget and did not force a new repair.

New leases declare `geometryOwner: "native"`. The private desktop still manages
its compositor's visibility, but does not replay bounds or perform geometry
pulses for these leases. The legacy bounds path remains for old shells.

During a native move/size loop, the existing DWM mirror supplies the manager's
visible editor surface. The independent input window stays alive and visible
offscreen; repeated pointer movements do not synchronously resize or reclip
another process. Only the mirror's destination follows the manager layout.
The lease's `interactiveMove` flag defers desktop presentation until the shell
finishes placement and clipping. On exit, fresh client coordinates and actual
window-region checks determine when to restore the input surface. Explicit
"관리창 안에 표시" uses the same forced repair path.

A borderless source may legitimately have no explicit region: that is accepted
only when its client insets are zero and its entire native window exactly fits
the viewport. Framed windows still require the exact client clipping rectangle.
Geometry and clipping have separate bounded retry budgets. If final placement
cannot be verified, a deadline restores the verified native window and reports
the recovery failure rather than leaving an unclickable mirror indefinitely.

This targets movement, sizing and recovery. The visible editor is scaled during
a resize, then settles to its actual layout when sizing ends. Conversations,
profile selection, drafts and model requests are unaffected.

Validation uses disposable offscreen native and Chromium windows. No existing
user app, account, conversation or SSH connection is selected by these fixtures.
Regression coverage includes delayed leases, mixed DPI lease values, source
parking, capture content during dragging, post-drag geometry, dropped clipping,
explicit repair, visibility restoration and a subsequent no-snapback check.

Window messages are exercised through the real root HWND after garbage
collection, rather than invoking the hook method directly. The native region
is measured in window-relative pixels, including client insets as required by
[SetWindowRgn](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setwindowrgn).

The real Chromium fixtures passed both `--frame-progress` and `--modal-popups`
scenarios. They verify the renderer's actual CSS viewport against native client
pixels, populated manager-only captures during dragging, final placement and
clipping, asynchronous restore after a transient lease write failure, and
subsequent minimize, profile hide/show and nested-modal transitions. Native
fixtures additionally cover framed sources, retry limits, failure recovery and
resuming a placement after time spent hidden. The desktop adapter VM suite and
13 bundle tests passed. These are offscreen automated checks; normal interactive
dragging on the user's monitors remains the final manual check after reopening.
