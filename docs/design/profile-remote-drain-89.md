# Profile-scoped remote drain (revision 89)

## Observed failure

The previous full-exit path closed Windows profile processes and the management
service, but did not stop detached private SSH listeners. Their process birth
identities survived a local full exit and an elevated relaunch. A changed subagent
configuration could therefore remain deferred even after a user pressed full exit.

Shared-record listeners intentionally do not enable the older exclusive-source
manifest. They cannot provide its managed idle/shutdown RPC evidence. Matching
existing configurations remain reusable; a missing idle proof does not mean a
network failure and does not authorize killing an apparently idle process.

## Explicit actions

The profile menu and window menu offer **SSH 작업 종료 후 설정 적용**. It queues
a remote-only job for the selected profile generation and all its recorded hosts.
The Windows task and other profile IDs are left alone. Active remote turns
finish before shutdown; only after process exit and release of the profile's
instance lock are proved does the existing immutable-definition preparation and
start path install the selected model/subagent configuration.

On new services, full exit closes local profiles and also queues remote stop-only
jobs. It cannot report completion while a remote job is pending or unverified.
A bounded UI wait returns an explanatory message while the durable job continues.
The title-bar X still detaches the shell only.

A new shell attached to an older service explicitly explains that this first
full exit is local-only. The backend must be replaced before the new remote
commands can run. A newly built shell does not upgrade an already-running service.

## Lifecycle and identity

- ProfileRestarts.schedule_remote: explicit command, expected generation,
  existing worker exclusion, durable per-profile job.
- UpdateHooks.begin_remote_reconcile: SSH admission gate and frozen host cohort;
  only this explicit entry opts into graceful_drain. Normal open and automatic
  updates retain the strict managed-idle path.
- Reconciliation records drain_requested and the exact observed process before
  dispatch. A lost reply cannot select a replacement process.
- remote_helpers/native_controller.py uses the audited app-server SIGHUP
  graceful-drain path through a Linux pidfd. No SIGTERM, SIGKILL, process-group
  kill or process-name matching is used by this path.
- The native-start lock serializes start versus drain. Process ID, birth time,
  boot ID, executable and private profile socket are checked. Unknown signal
  behavior is refused.
- Busy turns remain pending. Empty loaded-thread lists are not idle proof.
  Success requires process exit AND instance-lock release.
- Stop-only jobs do not prepare new definitions, start a listener, promote a
  policy revision, or clear a pending-settings notice as if settings were applied.
- Changed generation/policy or unexpected process identity stops the operation
  with its journal retained. Separately installed terminal Codex daemons and
  server-wide services are outside the selected profile scope.

Do not replace this with pkill, blanket process termination, permission changes,
or a stock app-server daemon update invocation. Those actions have a different
scope and do not solve the profile configuration lifecycle.

## Audited runtime and scope limits

The current signal path accepts only SHA-256
`4c6ca2dd15f100ea740ac01d956bb1898c1b37f6c5d9bfc269b405640cc6249a`,
matching the deployed `0.153.4-managed-e29fcb2680f61520` artifact. Before adding a
future binary to `DRAIN_AUDITED_SHA256` in `native_controller.py`, review its
actual app-server signal handler: SIGHUP must still select `GracefulOnly`, repeated
SIGHUP must not escalate, and shutdown must wait for active assistant turns.
The hash is checked against the running executable, with device/inode equality
to the descriptor's executable. Atomic replacement is refused; a filename or
embedded marker alone is not sufficient proof. Repeated polling of an already
signalled, exact process uses its pinned journal and does not hash it again.

The guarantee covers current assistant turns. It is not an exclusive all-client
transaction over queued input, external subprocesses or independent background
jobs. Finish or save such work before explicitly requesting a profile restart.
Automatic update and ordinary profile selection never opt into this signal path.

## Validation boundary

Tests use temporary state stores and synthetic process identities. They cover
waiting turns, lost replies, PID birth changes, changed generation/policy, two-host
partial completion, stop-only behavior, ordinary-open non-interference, and a
prepared host missing from inventory. Linux signal checks use only child
processes created by their test. Existing real profile tasks are not signalled
or restarted to validate this patch. A production profile's new configuration
must still be checked after the user applies it using the updated service.

The Linux drain fixture run completed 34 tests without skips. The Windows
run of that fixture skips 7 Linux-only checks. The final manager integration
suite covers 10 explicit per-profile lifecycle scenarios. The Windows release
build passed; runtime schema and frozen bundle hashes were checked before
publishing build `20260923-073019-732` for next launch.
