# Terminal Codex updater results (revision 80)

The terminal updater is separate from the workspace-managed SSH runtimes.
Never restart the latter to recover a terminal update.

## Failure

Codex 0.155.1 can successfully exit `app-server daemon update` with a JSON
`status: "unsupported"`. This occurs, for example, when the stable standalone
installation is absent or another application owns the daemon. Previously the
helper discarded stdout, interpreted exit code zero as a successful update,
then waited forever for the running version to match the CLI. The dialog kept
its confirmation checkbox disabled throughout that wait.

Reference: [official 0.155.1 manual updater](https://github.com/openai/codex/blob/rust-v0.155.1/codex-rs/app-server-daemon/src/manual_update.rs).

## Behavior

- Command help alone does not establish installation eligibility. A readable
  standalone version is required before enabling the update action.
- Capture bounded stdout and retain only documented status and version fields.
  `unsupported` is terminal, not successful and not pending verification.
  `updated` and `noUpdate` still require a service-version verification.
- A legacy successful-command journal with an exited worker and a demonstrably
  missing standalone installation can settle as unsupported under the update
  lock. No update command is replayed. Uncertain observations remain blocked.
- The dialog explains missing installation or installer ownership and offers
  an explicit copy action for the official standalone install command when
  applicable. It does not silently install, bootstrap, or restart anything.
- Once the installation changes, a fresh observation and work-finished
  confirmation can enable updating. The old confirmation is never reused.
- Terminal CLI probe failure preserves host metadata for independent workspace
  runtime checks, so an unrelated CLI failure cannot erase its platform target.

## npm installations

The same update button also supports a user-writable global `@openai/codex`
installation, including a shell wrapper around its launcher. Discovery verifies
the global package manifest and version. For a running server, the default
home's Unix socket peer must be the same user's Codex executable inside that
package (including npm's renamed old package directory). A different home or
any workspace-manager environment disqualifies it. The confirmed observation
includes its boot, birth time and socket peer identity.

The detached worker resolves the registry's latest version, installs that exact
version with npm when newer, and verifies the installed executable before
touching the old daemon. It never downgrades a newer local CLI. It rechecks the
socket identity, sends TERM through the previously opened pidfd, waits for exit,
and starts only the default terminal app-server. No blanket process matching,
SIGKILL, workspace profile restart or automatic replay is used. Boolean feature
overrides from the old terminal server are retained.

Success requires a verified npm socket peer and a daemon version matching the
installed CLI. A failed install leaves the old server running. Changed targets
require a fresh observation; uncertain stop/start results remain recorded for
verification. This path does not require converting the npm installation to
the official standalone layout.

## Validation

Python tests cover semantic results, metadata preservation and legacy journal
reconciliation. Isolated Linux tests run detached workers against fake CLIs,
including exit-zero unsupported and duplicate-confirmation cases. Windows UI
checks cover unsupported guidance, setup visibility and re-enabling after a
fresh supported observation. No production daemon update is part of tests.
The npm integration fixtures compile a small Unix-socket server and use an
isolated fake registry/installer to check real pidfd shutdown, restart, failure
preservation, workspace rejection, changed-peer rejection and downgrade guards.
