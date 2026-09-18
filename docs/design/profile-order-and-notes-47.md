# Profile order and note layout — revision 47

Profiles previously followed registration order. The sidebar now has a drag grip
on each profile, an insertion line, edge scrolling, and an alternative right-click
menu (move up/down). Alt+Up/Down moves the keyboard-selected row. Dragging the grip
does not select or launch its profile. Escape or dropping outside cancels.

`profile.move` carries a source ID, target ID, and before/after position. The Python
store applies that relative operation while holding its existing interprocess
lock. It reorders the existing profile objects, preserving login, usage, process,
source, shortcut, and representative-account data. New accounts append; removed
accounts stay recoverable. Invalid/removed targets fail without writing. The Rust
service admits the new command. No schema or IPC version change is required.

The shell pauses profile refresh during dragging/saving and invalidates earlier
state responses. The saved order is applied with selection events suppressed;
opening a profile remains a separate action. No current user session was switched
or restarted during development or verification.

Notes use panel-specific spacing instead of the large shared form margins:
separate new-note/menu toolbar and wrapping tabs, compact text area, fixed checkbox
and delete columns aligned with the first text line, dark-theme checkboxes,
subtle add-item action, bordered document, and a bottom save/completion status.
Existing body/items, note IDs, auto-save, conflicts, and recovery are unchanged.

Validation:

- Five Python order tests: command dispatch, reload persistence, upward/downward
  moves, adding an API profile, invalid targets, removal/restoration, and concurrent
  account updates/additions. Existing 22 control-center tests pass.
- Eleven isolated WPF ordering checks: drag targets, single submission, click vs
  drag, outside/cancel, boundaries, menu/keyboard movement, selection preservation,
  and save lifetime.
- Nineteen real WPF + Rust notes checks pass, including mixed body/check edits,
  legacy notes, completion, removal, task switching, draft recovery, and conflicts.
- Full `scripts/build-manager.ps1 -SelfTest` passes native viewport, caption,
  responsiveness, notification, notes, and profile-order checks.
- Rendered `work/control-center-notes-test.png` and
  `work/control-center-profile-order-test.png` inspected for layout/contrast.

Release: `artifacts/manager/releases/20260918-064158-173`. Existing launchers select
this build on the next normal manager launch. No profile restart is needed for
this shell-only UI and manager-state change.
