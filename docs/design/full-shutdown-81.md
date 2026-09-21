# Full shutdown with pending SSH update journals (revision 81)

## Failure

`RemoteUpdates.status_all().worker_active` includes durable queued/waiting jobs
and stock-update verification journals, even when no callback is executing.
The old full-exit path closed the managed desktop processes, then retried passive
`supervisor.retire` for eight seconds. A waiting SSH journal kept `can_stop` false
indefinitely. Retrying could not help. The failed exit also left profile warmup's
stop flag set, so reopening the shell reported that the service was shutting down.

## Behavior

- The title-bar X still closes only the shell and preserves live work.
- Explicit full exit first blocks new local launches, drains admitted launches,
  closes native windows, and cleans up each verified profile generation.
- Revision 81 advertises `graceful_shutdown` in `supervisor.status`. The shell then
  uses `supervisor.shutdown`, distinct from passive retirement during upgrades.
- The service holds its retirement/admission locks, requires a single client,
  exited profiles and no unfinished non-SSH management operation. Only the saved
  SSH queue is allowed to remain at this point.
- It closes the adapter's stdin. The existing Python finalizer stops scheduling,
  skips not-yet-started callbacks and lets admitted executor work finish. No queue
  records, saved preferences, credentials or notes are deleted. No process is
  force-killed and no remote lifecycle request is replayed.
- The service remains in a draining state until the adapter actually exits. New
  profile/management requests are rejected; authenticated process-broker requests
  needed by admitted cleanup remain available. Pending exit can be retried without
  constructing another adapter. The shell verifies service process exit too.
- Shutdown progress reports the actual busy response, with a bounded 60-second
  wait instead of discarding the reason after eight seconds. A pending drain keeps
  normal polling/launching stopped. A failure before draining restores launch and
  warmup admission without reopening profiles that the user already closed.

## Upgrading an already-running older service

Fixing only the new service cannot unblock a user still running the old one.
The new shell therefore supports the old Rust service's existing graceful
`supervisor.reconnect` (adapter EOF and verified exit), followed by passive
`supervisor.retire`. It checks fresh profile, launch-barrier, management, client
and pending-request state before entering this compatibility path. It does not
clear SSH jobs or change auto-update settings. This fallback is used only for
explicit full exit, never for ordinary window close or automatic upgrade.

To apply: close the old shell with X, reopen the workspace launcher to load the
new shell, then use full exit. The next launch starts the new service as well.

## Verification

- Rust integration starts a real isolated Python adapter with a waiting journal;
  passive retirement refuses, explicit exit waits for normal EOF completion,
  rejects new requests while draining and preserves the journal byte-for-byte.
- Shutdown predicate tests preserve running/unknown-profile and active-launch/
  management protections. Multiple connected clients block shutdown.
- WPF shell tests cover old/new service routing, a slow adapter, timeout detail,
  retry without backend respawn, and normal X behavior.
- Python tests cover cancelled callbacks, preserved SSH journals, and warmup
  recovery both before and after an admitted warmup pass has finished.
- Production workspace/profile processes are not stopped as part of tests.

This change does not update or stop a separately installed SSH terminal Codex
daemon. Existing remote update journals are reconciled normally on next startup.
