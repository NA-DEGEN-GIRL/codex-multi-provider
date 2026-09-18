# Settings dialogs and profile defaults (52)

The manager used `IsEnabled` / `IsWindowEnabled(owner)` as visibility conditions
for its independent native viewport. WPF `ShowDialog()` disables its owner, so
every settings dialog hid both the Codex window and the DWM capture mirror. It
also changed the presentation epoch, unnecessarily repeating compositor setup
on close.

`NativeWindowHost` now separates presentation from modal input handling. While
the selected viewport and manager remain visible, the compositor and mirror
remain visible. When the manager is disabled, the native window is positioned
immediately behind the manager. The disabled manager and its owned dialogs cover
the input surface while the DWM mirror supplies the live background. After the
last dialog closes, normal viewport ordering resumes. No cross-process owner,
parent, keyboard forwarding, synchronous EnableWindow, activation or renderer
reload is added. Minimized managers and inactive profiles still hide their
viewports and mirrors.

The profile context menu and profile management menu now expose **하위 에이전트
설정**. The profile ID is captured before awaiting registry data; closing the
context menu or changing selection cannot retarget the dialog. Its heading names
the profile being edited. The menu snapshots its profile on open because WPF
can close a popup before dispatching `MenuItem.Click`. The external model dialog labels reasoning effort as
**기본 추론 강도** and explains that an explicit chat selection takes priority
without changing the stored profile default.

Validation:

- Real isolated Codex 26.915.3509.0 + production WPF host at 144 DPI, empty home,
  no credentials or model calls: `artifacts/results/desktop-render-dfa16e21/`.
- Actual `ShowDialog()` plus nested modal: native owner disabled, source visible
  behind manager, populated window-only capture, unchanged presentation epoch,
  and unchanged foreground. Final close restores native input window order.
- Capture negative control: removing DWM mirror produces a single-color empty
  viewport; normal capture has 2,990 colors. Resize, hide/show, minimize/restore
  and lost-lease-publication recovery also pass.
- Native fixture checks a delayed TOPMOST show during modal state and correct
  ordering after re-enable. Desktop-container reorder events from the attached
  PID now trigger layout recovery. Four external-profile effort routing tests pass.
- The isolated desktop rendering harness now sets an explicit selected CLI path,
  like production. Otherwise this desktop version attempts an MSIX-only runtime
  bootstrap in the unpackaged fixture.

DWM relationship reference:
https://learn.microsoft.com/en-us/windows/win32/api/dwmapi/nf-dwmapi-dwmregisterthumbnail
