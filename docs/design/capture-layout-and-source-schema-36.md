# Capture, workspace layout, and legacy source catalogs — revision 36

## Window capture

The independent Electron HWND introduced for native inline Korean composition
was visible on the desktop but absent from captures of the manager HWND alone.
The manager therefore exposed its gray placeholder in those captures.

`NativeWindowHostCaptureMirror` registers a live DWM thumbnail on the manager
surface beneath the independent input window. It follows viewport geometry and
visibility and is disposed with the attachment. It does not reparent the input
window, change ownership, activate it, or forward keyboard input. No frames are
saved or transmitted. Electron also invalidates its compositor once per
presentation epoch, without reloading or repeatedly repainting an idle view.

Real isolated Electron verification:
`artifacts/results/desktop-render-8bf1d17e/wpf-host.json`.
Manager-only PrintWindow captures contained 845 colors. Removing the mirror
reproduced the single-color blank viewport. Resize, quick and slow minimize /
restore, hiding and reselecting the profile retained the captured content.

## Workspace layout

Agent UX review and the imagegen reference board favored the sidebar layout.
Profiles and task shortcuts stay in a 272-DIP sidebar. Account, connection,
shortcut, and maintenance actions are grouped in flat menus or disclosures.
Active profile and task remain visible; diagnostics and healthy runtime details
are collapsed by default. Actionable warnings remain visible. Notes and detailed
logs start closed, while log copy remains directly available. Selection survives
hover, and disclosures / scrollbars use the dark palette.

Design assets: `artifacts/design/workspace-36/`.
The implementation image uses synthetic profiles, not the user's session.
Layout checks cover 1440×960 and the 1024×840 minimum. Notes validation opens
the panel through its public button and exercises persistence / draft recovery.

## Legacy source database compatibility

Native logs repeatedly failed remote catalogs on `remote-linux` and `remote-c` with
`no such table: thread_sections`. Their stock source stores predate sections
and projects; managed stores have the newer schema. The read-only source reader
incorrectly used the writable runtime's latest column list unconditionally.

The reader now inspects optional organization columns and builds query-local
CTEs for missing nullable fields and absent section presentation metadata.
Existing fields are preserved, and required schema / invalid references still
produce errors. Descendant CTEs, filters, cursor pagination and current writable
query plans remain intact. Legacy stores report no named projects. No source
database migrations, copies, table creation, or metadata rewriting occur.

Validation:

- All 190 `codex-state` tests pass, including schema versions 44, 47 and 52,
  all read entrypoints, section/project filters, relationship queries, cursor
  pagination, and byte-for-byte unchanged source files.
- Windows candidate `20260917-134614-c65ce4` passes shared-editing pagination
  (`shared-editing-f618cda4`) and four-client canonical storage
  (`canonical-store-27ed6119`) integration tests with local fixture responses.
- Manager release `20260917-134712-042` passes native host, titlebar, notification
  and notes checks. Running user instances were not restarted or navigated.
- Linux bundle `0.153.4-managed-cdccc2d60934efe4-f9b1076fc9a59265` passes actual
  read-only catalog and history probes on remote-dev (6,822 records), remote-linux
  (127 legacy-schema records), and hp (8 legacy-schema records). It is published
  for future preparation with the previous bundle preserved. Evidence is in
  `artifacts/results/schema36-remote-probes-20260917-225340/` and
  `artifacts/results/mixed-catalog-probe-20260917-225007/report.json`.

The remote-linux filesystem has only about 312 MB free while the new program
files need about 475 MB. Its verification used a private RAM-backed temporary
directory and removed that staging afterward. Production installation there
still requires free disk space; no user files or installed runtimes were removed.

New binaries and bundles are selected for future launches. A running process
continues using its loaded code until it exits; publication is not a live patch.
