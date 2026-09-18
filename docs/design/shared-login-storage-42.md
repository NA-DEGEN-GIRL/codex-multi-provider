# Common history from the first login

Fix 41 repaired the MSIX executable launch but still opened a login-only runtime
without CODEX_RECORD_HOME. Profile 02's local record database was empty while the
canonical database retained 2,168 records. SSH history remained visible because
it was queried from its remote hosts. The same omission affected newly added
profiles. Requiring manual verification and reopening left users in that mode.

When canonical storage is supported, new login and reauthentication now use the
managed runtime from startup, with canonical record/SQLite paths and the usual
shared-record signaling. Authentication and configuration stay in the private
profile HOME. An explicit native login phase permits OAuth start/cancel/logout
and local setup. The account guard blocks work until a saved identity exists,
checks existing profile identity, and binds a new profile's running process once.
Subsequent identity changes cannot silently start work under another account.

The current runtime publishes a non-secret identity fingerprint. State refresh
accepts matching saved credentials only from that generation, completes the login
phase locally, and keeps the same desktop and runtime. No verification probe,
window restart, navigation, or task creation is needed for history access. A local
credential observation is labeled separately from server verification.

Old packaged login profiles are moved to this mode on their next cold open. No
active window is closed by migration and no saved credentials are copied/deleted.
Builds lacking canonical-store support retain their prior login implementation.

Validation:
- 117 focused Python tests: account isolation, environment separation, migration,
  login completion/generation, proxy transport, cache and startup behavior.
- `scripts/test_shared_login_headless.py` with real managed CLI and production
  Python proxy: new-account and relogin OAuth start/cancel; shared project/list/body
  before login; simulated saved fixture identity; continued writes in the same
  process; original-store reads of both updates. Three loopback model requests,
  zero real model requests, no user windows or credentials touched.
- Evidence: `artifacts/results/shared-login-20d329ea/report.json`.

Browser OAuth completion and interactive desktop acceptance remain user checks.
