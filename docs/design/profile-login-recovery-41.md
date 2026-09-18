# Profile login recovery 41

Profile 02 had an expired borrowed access token. Its executing runtime rejected
an in-app account change, and the manager correctly selected isolated native
login, but that launch used the installed MSIX CLI directly from WindowsApps.
The unpackaged desktop could read that executable but could not execute it:
direct `--version` failed with WinError 5, surfaced by the desktop as spawn EPERM.

Native login now uses the same SHA-256 verified official CLI staging path as
account verification. Its profile-local CODEX_HOME and file credential store stay
isolated; borrowed credentials and account guards are not inherited by the login
process. Existing account fingerprint verification still rejects a different
account. After verification, the existing flow selects managed runtime for the
next open, where the profile's native OAuth credentials can refresh themselves.

Login launches that exit during initial startup now return an error instead of
reporting a ready login window. Account-bound errors explain how to reauthenticate
the same profile, instead of telling the user to choose another profile.

Validation: 69 focused Python tests passed, including byte identity, staging cache
reuse/corruption, startup exit, and account isolation. `tests/login_runtime_live.py`
used the production environment builder and real official CLI with a disposable
empty HOME: initialize and signed-out account/read passed. No browser OAuth login
was performed, and no running user profile was restarted or navigated.
